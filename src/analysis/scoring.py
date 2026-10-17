"""Scoring engine (PRD 7).

Produces per-dimension scores in 0..100 and a weighted total. Weights and
normalization reference points come from scoring_config.yaml. A high total
score with low confidence is NOT auto-promoted (caller/UI responsibility);
this module exposes confidence alongside the score.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..models.enums import Confidence


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class ScoreInputs:
    """Raw inputs for scoring a candidate.

    All fields are optional; missing data lowers confidence and avoids awarding
    high scores for absent information (PRD 7.5, 7.7).
    """

    demand_index: Optional[float] = None       # 0..index_max
    growth_pct: Optional[float] = None          # may be None (insufficient data)
    price_gap_ratio: Optional[float] = None     # (domestic - landed)/domestic
    margin_pct: Optional[float] = None          # net margin pct
    competitor_count: Optional[int] = None      # number of domestic sellers


@dataclass(frozen=True)
class ScoreResult:
    demand_score: float
    growth_score: float
    price_score: float
    margin_score: float
    competition_score: float
    total_score: float
    confidence: Confidence
    risk_flags: list[str] = field(default_factory=list)


class Scorer:
    """Computes dimension scores and the weighted total from config."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.weights = config["weights"]
        self.norm = config.get("normalization", {})

    # --- per-dimension scoring (each returns 0..100) ---

    def score_demand(self, demand_index: Optional[float]) -> float:
        if demand_index is None:
            return 0.0
        index_max = float(self.norm.get("demand_index_max", 100.0))
        if index_max <= 0:
            return 0.0
        return _clamp(demand_index / index_max * 100.0)

    def score_growth(self, growth_pct: Optional[float]) -> float:
        if growth_pct is None:
            return 0.0
        full = float(self.norm.get("growth_pct_full_score", 50.0))
        if full <= 0:
            return 0.0
        # Negative growth clamps to 0; growth >= full yields 100.
        return _clamp(growth_pct / full * 100.0)

    def score_price(self, price_gap_ratio: Optional[float]) -> float:
        if price_gap_ratio is None:
            return 0.0
        full = float(self.norm.get("price_gap_full_score", 0.5))
        if full <= 0:
            return 0.0
        return _clamp(price_gap_ratio / full * 100.0)

    def score_margin(self, margin_pct: Optional[float]) -> float:
        if margin_pct is None:
            return 0.0
        full = float(self.norm.get("margin_pct_full_score", 40.0))
        if full <= 0:
            return 0.0
        return _clamp(margin_pct / full * 100.0)

    def score_competition(self, competitor_count: Optional[int]) -> float:
        """Fewer domestic sellers => higher score (less competition)."""
        if competitor_count is None:
            return 0.0
        max_sellers = float(self.norm.get("competition_sellers_max", 20))
        if max_sellers <= 0:
            return 0.0
        # 0 sellers -> 100; at/above max -> 0.
        score = (1.0 - (competitor_count / max_sellers)) * 100.0
        return _clamp(score)

    # --- confidence & risk flags ---

    @staticmethod
    def assess_confidence(inputs: ScoreInputs) -> tuple[Confidence, list[str]]:
        """Derive a confidence level and risk flags from data completeness."""
        flags: list[str] = []
        present = 0
        total = 5

        if inputs.demand_index is None:
            flags.append("missing_demand_data")
        else:
            present += 1
        if inputs.growth_pct is None:
            flags.append("insufficient_growth_data")
        else:
            present += 1
        if inputs.price_gap_ratio is None:
            flags.append("missing_price_data")
        else:
            present += 1
        if inputs.margin_pct is None:
            flags.append("missing_margin_data")
        else:
            present += 1
        if inputs.competitor_count is None:
            flags.append("missing_competition_data")
        else:
            present += 1

        if present == total:
            confidence = Confidence.PARTIALLY_VERIFIED
        elif present >= 3:
            confidence = Confidence.UNVERIFIED
        else:
            confidence = Confidence.INSUFFICIENT_DATA
        return confidence, flags

    # --- total ---

    def score(self, inputs: ScoreInputs) -> ScoreResult:
        demand = self.score_demand(inputs.demand_index)
        growth = self.score_growth(inputs.growth_pct)
        price = self.score_price(inputs.price_gap_ratio)
        margin = self.score_margin(inputs.margin_pct)
        competition = self.score_competition(inputs.competitor_count)

        total = (
            demand * float(self.weights["demand"])
            + growth * float(self.weights["growth"])
            + price * float(self.weights["price"])
            + margin * float(self.weights["margin"])
            + competition * float(self.weights["competition"])
        )
        total = _clamp(total)

        confidence, flags = self.assess_confidence(inputs)

        return ScoreResult(
            demand_score=round(demand, 2),
            growth_score=round(growth, 2),
            price_score=round(price, 2),
            margin_score=round(margin, 2),
            competition_score=round(competition, 2),
            total_score=round(total, 2),
            confidence=confidence,
            risk_flags=flags,
        )
