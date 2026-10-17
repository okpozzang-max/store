"""Demand-side models: data sources and demand signals (PRD 6.1, 6.2)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from .enums import Confidence, SignalType


class DataSource(BaseModel):
    """A registered data source (PRD 6.1).

    Secrets (API keys) are NEVER stored here; only metadata about the source.
    """

    id: Optional[int] = None
    name: str
    source_type: str  # e.g. "search_trend", "shopping_insight", "csv", "stats"
    country: Optional[str] = None
    access_method: Optional[str] = None  # "api" | "csv" | "manual" | "browser"
    terms_url: Optional[str] = None
    license_notes: Optional[str] = None
    enabled: bool = True
    last_success_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None


class DemandSignal(BaseModel):
    """A single demand observation (PRD 6.2).

    ``demand_index`` is a provider-relative measure (e.g. search interest),
    distinct from actual sales. ``signal_type`` records which kind it is so it
    is never mistaken for an absolute sales figure (PRD 7.2).
    """

    id: Optional[int] = None
    keyword: str
    category: Optional[str] = None
    country: Optional[str] = None
    source_id: Optional[int] = None
    observed_at: Optional[datetime] = None
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None
    demand_index: float = Field(..., ge=0.0)
    growth_pct: Optional[float] = None
    signal_type: SignalType = SignalType.SEARCH_INTEREST
    confidence: Confidence = Confidence.UNVERIFIED
    raw_reference: Optional[str] = None  # source URL or reference string

    @field_validator("keyword")
    @classmethod
    def _keyword_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("keyword must not be blank")
        return v.strip()
