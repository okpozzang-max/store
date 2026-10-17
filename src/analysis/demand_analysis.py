"""Demand analysis: growth rate and demand normalization (PRD 7.2, 7.3).

Signals from different sources use different scales, so they are normalized
before comparison and never naively summed. Growth is computed defensively:
a zero/missing baseline yields an 'insufficient data' result rather than
infinity.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class GrowthResult:
    """Result of a growth computation.

    ``growth_pct`` is None when the baseline is zero/missing; in that case
    ``insufficient_data`` is True (PRD 7.3).
    """

    growth_pct: Optional[float]
    insufficient_data: bool
    reason: Optional[str] = None


def compute_growth(recent_value: float, previous_value: float) -> GrowthResult:
    """Compute period-over-period growth percentage.

    growth_pct = (recent - previous) / previous * 100

    A zero or negative baseline is treated as insufficient data rather than
    producing an infinite or misleading growth figure.
    """
    if previous_value is None or recent_value is None:
        return GrowthResult(None, True, "missing value")
    if previous_value <= 0:
        return GrowthResult(None, True, "baseline value is zero or negative")
    growth = (recent_value - previous_value) / previous_value * 100.0
    return GrowthResult(round(growth, 2), False)


def normalize_demand_index(demand_index: float, index_max: float = 100.0) -> float:
    """Map a provider-relative demand index onto 0..100.

    Values are clamped into range so a single source cannot exceed 100.
    """
    if index_max <= 0:
        return 0.0
    scaled = demand_index / index_max * 100.0
    return _clamp(scaled, 0.0, 100.0)


def aggregate_demand_indices(indices: list[float], index_max: float = 100.0) -> float:
    """Combine multiple normalized source indices into one 0..100 figure.

    Uses the mean of per-source normalized values (not a raw sum) so differing
    scales do not distort the aggregate (PRD 7.2).
    """
    if not indices:
        return 0.0
    normalized = [normalize_demand_index(i, index_max) for i in indices]
    return round(sum(normalized) / len(normalized), 2)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
