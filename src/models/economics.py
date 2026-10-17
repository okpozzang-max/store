"""Economics & result models (PRD 6.6, 6.7, 8).

Covers the cost inputs/outputs for unit economics, the opportunity (scoring
result) record, and collection run logging.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from .enums import Confidence, ReviewStatus, RunStatus


class CostInputs(BaseModel):
    """Inputs for a landed-cost / profitability calculation (PRD 8.1).

    Each monetary field is in its stated currency; ``exchange_rate`` converts
    the foreign purchase price (and foreign shipping) into the base currency.
    Fees expressed as a fraction of the sale price are kept separate from
    costs added to the landed cost (PRD 8.2).
    """

    foreign_price: float = Field(..., gt=0.0)
    currency: str
    exchange_rate: float = Field(..., gt=0.0)  # 1 unit currency -> KRW

    foreign_shipping_cost: float = Field(default=0.0, ge=0.0)  # in `currency`
    international_shipping_krw: float = Field(default=0.0, ge=0.0)
    domestic_shipping_krw: float = Field(default=0.0, ge=0.0)

    # Fees as fractions of the respective base (0.03 == 3%).
    payment_fee_pct: float = Field(default=0.0, ge=0.0, le=1.0)   # on overseas payment
    platform_fee_pct: float = Field(default=0.0, ge=0.0, le=1.0)  # on sale price
    return_allowance_pct: float = Field(default=0.0, ge=0.0, le=1.0)  # on sale price

    customs_tax_krw: float = Field(default=0.0, ge=0.0)  # estimated duties/tax
    fx_buffer_pct: float = Field(default=0.0, ge=0.0, le=1.0)  # on overseas payment
    other_costs_krw: float = Field(default=0.0, ge=0.0)

    domestic_sale_price_krw: float = Field(..., gt=0.0)


class CostResult(BaseModel):
    """Output of a profitability calculation (PRD 8.2)."""

    product_cost_krw: float
    cost_before_sale_fees_krw: float = 0.0
    total_cost_krw: float
    estimated_profit_krw: float
    estimated_margin_pct: float
    required_sale_price_krw: Optional[float] = None
    # Itemized breakdown for transparency (every recommendation shows its basis).
    breakdown: dict[str, float] = Field(default_factory=dict)


class Opportunity(BaseModel):
    """A scored sourcing candidate (PRD 6.6)."""

    id: Optional[int] = None
    product_id: int
    supplier_offer_id: Optional[int] = None
    domestic_offer_id: Optional[int] = None

    demand_score: float = Field(..., ge=0.0, le=100.0)
    growth_score: float = Field(..., ge=0.0, le=100.0)
    price_score: float = Field(..., ge=0.0, le=100.0)
    margin_score: float = Field(..., ge=0.0, le=100.0)
    competition_score: float = Field(..., ge=0.0, le=100.0)
    total_score: float = Field(..., ge=0.0, le=100.0)

    estimated_profit_krw: Optional[float] = None
    estimated_margin_pct: Optional[float] = None
    risk_flags: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.UNVERIFIED
    review_status: ReviewStatus = ReviewStatus.NEW
    calculated_at: Optional[datetime] = None
    # Explanation of how the score was derived: inputs, sources, linked
    # keywords, cost breakdown, fx provenance (PRD 1.2, 14 #9).
    score_basis: dict[str, Any] = Field(default_factory=dict)


class CollectionRun(BaseModel):
    """A record of a collection/analysis run (PRD 6.7)."""

    id: Optional[int] = None
    source_id: Optional[int] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    status: RunStatus = RunStatus.RUNNING
    records_collected: int = 0
    records_failed: int = 0
    error_summary: Optional[str] = None
