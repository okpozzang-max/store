"""Scoring and demand analysis tests (PRD 12 unit tests #4, #6)."""
from __future__ import annotations

import pytest

from src.analysis.demand_analysis import (
    aggregate_demand_indices,
    compute_growth,
    normalize_demand_index,
)
from src.analysis.scoring import ScoreInputs, Scorer
from src.models.enums import Confidence


def test_scores_within_range(scoring_config):
    scorer = Scorer(scoring_config)
    # Extreme inputs must still yield 0..100.
    result = scorer.score(
        ScoreInputs(
            demand_index=99999,
            growth_pct=99999,
            price_gap_ratio=5.0,
            margin_pct=9999,
            competitor_count=0,
        )
    )
    for field in (
        result.demand_score,
        result.growth_score,
        result.price_score,
        result.margin_score,
        result.competition_score,
        result.total_score,
    ):
        assert 0.0 <= field <= 100.0


def test_scores_floor_at_zero(scoring_config):
    scorer = Scorer(scoring_config)
    result = scorer.score(
        ScoreInputs(
            demand_index=0,
            growth_pct=-100,
            price_gap_ratio=-1.0,
            margin_pct=-50,
            competitor_count=100,
        )
    )
    assert result.demand_score == 0.0
    assert result.growth_score == 0.0
    assert result.price_score == 0.0
    assert result.margin_score == 0.0
    assert result.competition_score == 0.0
    assert result.total_score == 0.0


def test_missing_data_lowers_confidence(scoring_config):
    scorer = Scorer(scoring_config)
    result = scorer.score(ScoreInputs())  # all None
    assert result.confidence == Confidence.INSUFFICIENT_DATA
    assert "missing_demand_data" in result.risk_flags
    assert result.total_score == 0.0


def test_full_data_partially_verified(scoring_config):
    scorer = Scorer(scoring_config)
    result = scorer.score(
        ScoreInputs(
            demand_index=50,
            growth_pct=10,
            price_gap_ratio=0.2,
            margin_pct=20,
            competitor_count=5,
        )
    )
    assert result.confidence == Confidence.PARTIALLY_VERIFIED
    assert result.risk_flags == []


def test_weighted_total(scoring_config):
    scorer = Scorer(scoring_config)
    # All dimension scores 100 => weighted total 100.
    result = scorer.score(
        ScoreInputs(
            demand_index=100,
            growth_pct=50,
            price_gap_ratio=0.5,
            margin_pct=40,
            competitor_count=0,
        )
    )
    assert result.total_score == pytest.approx(100.0)


# --- growth (PRD 7.3 / 12 #6) ---

def test_growth_normal():
    r = compute_growth(150, 100)
    assert r.growth_pct == pytest.approx(50.0)
    assert r.insufficient_data is False


def test_growth_zero_baseline():
    r = compute_growth(100, 0)
    assert r.growth_pct is None
    assert r.insufficient_data is True


def test_growth_negative_baseline():
    r = compute_growth(100, -5)
    assert r.insufficient_data is True


def test_growth_missing_value():
    r = compute_growth(None, 100)
    assert r.insufficient_data is True


def test_normalize_and_aggregate():
    assert normalize_demand_index(150, 100) == 100.0  # clamped
    assert normalize_demand_index(50, 100) == 50.0
    assert aggregate_demand_indices([100, 0]) == 50.0
    assert aggregate_demand_indices([]) == 0.0
