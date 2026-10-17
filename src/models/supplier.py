"""Offer and supplier models (PRD 4.2, 6.4, 6.5, 9.4)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from .enums import AvailabilityStatus, SupplierType, TrustLevel, VerificationStatus


class SupplierOffer(BaseModel):
    """A foreign supplier offer for a product (PRD 6.4).

    Prices are preserved per observation: a price change creates a NEW row;
    an unchanged price only refreshes ``price_checked_at`` of the latest row.
    """

    id: Optional[int] = None
    product_id: int
    source_id: Optional[int] = None
    supplier_name: str
    supplier_country: Optional[str] = None
    product_url: Optional[str] = None
    foreign_price: float = Field(..., gt=0.0)
    currency: str
    availability_status: AvailabilityStatus = AvailabilityStatus.UNKNOWN
    shipping_cost: float = Field(default=0.0, ge=0.0)
    shipping_currency: Optional[str] = None
    price_checked_at: Optional[datetime] = None
    match_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED

    # Phase 3: provenance and evidence for search-collected offers.
    supplier_type: SupplierType = SupplierType.UNKNOWN
    listing_title: Optional[str] = None
    external_id: Optional[str] = None          # marketplace item id / item code
    item_condition: Optional[str] = None        # "new" | "used" | raw marketplace text
    shipping_cost_known: bool = True            # False => shipping not stated by the source
    ships_to_kr: Optional[bool] = None          # None => unknown
    seller_rating: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    seller_rating_count: Optional[int] = None
    match_method: Optional[str] = None
    match_reason: Optional[str] = None
    search_query: Optional[str] = None
    lead_time_days: Optional[int] = Field(default=None, ge=0)


class DomesticOffer(BaseModel):
    """A domestic (KRW) comparison offer (PRD 6.5)."""

    id: Optional[int] = None
    product_id: int
    seller_name: str
    product_url: Optional[str] = None
    price_krw: float = Field(..., gt=0.0)
    shipping_cost_krw: float = Field(default=0.0, ge=0.0)
    product_condition: Optional[str] = None  # "new" | "used" | ...
    match_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    price_checked_at: Optional[datetime] = None

    # Phase 3
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    source_id: Optional[int] = None
    listing_title: Optional[str] = None
    external_id: Optional[str] = None
    shipping_cost_known: bool = True
    match_method: Optional[str] = None
    match_reason: Optional[str] = None

    @property
    def total_price_krw(self) -> float:
        """Delivered domestic price including shipping."""
        return self.price_krw + self.shipping_cost_krw


class Supplier(BaseModel):
    """Supplier-level attributes managed for trust assessment (PRD 9.4)."""

    id: Optional[int] = None
    name: str
    country: Optional[str] = None
    supplier_type: SupplierType = SupplierType.UNKNOWN
    website: Optional[str] = None
    trust_level: TrustLevel = TrustLevel.NORMAL
    ships_to_kr: Optional[bool] = None
    return_policy: Optional[str] = None
    purchase_restrictions: Optional[str] = None
    authenticity_notes: Optional[str] = None  # 정품/지식재산권 관련 메모
    notes: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
