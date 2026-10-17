"""Product model: normalized product identity (PRD 6.3)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from .enums import KeywordLinkStatus


class Product(BaseModel):
    """Normalized product.

    ``gtin`` and ``model_number`` may be unknown, so both are nullable
    (PRD 6.3). Matching logic degrades gracefully when identifiers are absent.
    """

    id: Optional[int] = None
    brand: Optional[str] = None
    product_name: str
    model_number: Optional[str] = None
    gtin: Optional[str] = None
    category: Optional[str] = None
    variant: Optional[str] = None  # color / size / generation
    country_of_origin: Optional[str] = None
    # Phase 3 review: shipping weight for forwarder cost; the seller's own
    # intended sale price (overrides the domestic lowest price as sale price).
    weight_kg: Optional[float] = Field(default=None, gt=0.0)
    my_sale_price_krw: Optional[float] = Field(default=None, gt=0.0)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_validator("product_name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("product_name must not be blank")
        return v.strip()

    @field_validator("gtin")
    @classmethod
    def _gtin_digits(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = v.strip()
        if cleaned == "":
            return None
        if not cleaned.isdigit():
            raise ValueError("gtin must contain digits only")
        return cleaned


class ProductKeyword(BaseModel):
    """A keyword associated with a product (Phase 2).

    ``origin`` is 'generated' (rule-based candidate from product attributes),
    'demand_match' (matched from an observed demand keyword) or 'manual'.
    """

    id: Optional[int] = None
    product_id: int
    keyword: str
    normalized_keyword: str
    origin: str = "generated"
    match_method: Optional[str] = None
    match_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: KeywordLinkStatus = KeywordLinkStatus.CANDIDATE
    reason: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
