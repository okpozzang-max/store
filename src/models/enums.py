"""Shared enumerations used across data models (PRD sections 6, 7)."""
from __future__ import annotations

from enum import Enum


class SignalType(str, Enum):
    """Type of demand signal. Search interest != actual sales volume."""

    SEARCH_INTEREST = "search_interest"
    SHOPPING_INTEREST = "shopping_interest"
    TREND = "trend"
    IMPORT_STATS = "import_stats"
    MANUAL = "manual"


class Confidence(str, Enum):
    """Data confidence level (PRD section 7.7)."""

    VERIFIED = "verified"
    PARTIALLY_VERIFIED = "partially_verified"
    UNVERIFIED = "unverified"
    INSUFFICIENT_DATA = "insufficient_data"


class VerificationStatus(str, Enum):
    """Offer verification / identity status (PRD 6.4, 9.3, 9.4).

    VERIFIED      user confirmed (or the CSV says it was checked by hand)
    UNVERIFIED    automatically matched with strong evidence; used in pricing
                  but flagged as unverified
    NEEDS_REVIEW  identity uncertain; NOT used in price comparison
    REJECTED      user rejected; never used and never resurrected by searches
    """

    VERIFIED = "verified"
    NEEDS_REVIEW = "needs_review"
    UNVERIFIED = "unverified"
    REJECTED = "rejected"


PRICING_EXCLUDED_STATUSES = (VerificationStatus.NEEDS_REVIEW, VerificationStatus.REJECTED)
USER_OFFER_DECISIONS = (VerificationStatus.VERIFIED, VerificationStatus.REJECTED)


class SupplierType(str, Enum):
    """Supplier classification (PRD 4.2)."""

    OFFICIAL_STORE = "official_store"
    AUTHORIZED_RETAILER = "authorized_retailer"
    SPECIALTY_RETAILER = "specialty_retailer"
    WHOLESALE = "wholesale"
    MARKETPLACE_SELLER = "marketplace_seller"
    PRICE_COMPARISON = "price_comparison"
    UNKNOWN = "unknown"


class TrustLevel(str, Enum):
    """User-assigned supplier trust (PRD 9.4)."""

    TRUSTED = "trusted"
    NORMAL = "normal"
    BLOCKED = "blocked"


class AvailabilityStatus(str, Enum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"


class ReviewStatus(str, Enum):
    """Opportunity review workflow status (PRD section 6.6 / 10)."""

    NEW = "new"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class RunStatus(str, Enum):
    """Collection run status (PRD section 6.7)."""

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"  # e.g. source disabled or credentials not configured


class KeywordLinkStatus(str, Enum):
    """Status of a keyword <-> product link (PRD 9.1, 9.3).

    Only AUTO_LINKED and CONFIRMED links feed into demand scoring. Uncertain
    matches are NEEDS_REVIEW and never auto-applied. User decisions
    (CONFIRMED / REJECTED) are never overwritten by automatic matching.
    """

    CANDIDATE = "candidate"        # generated query term, not yet matched to demand
    AUTO_LINKED = "auto_linked"    # high-confidence automatic match
    NEEDS_REVIEW = "needs_review"  # uncertain; requires manual decision
    CONFIRMED = "confirmed"        # user confirmed
    REJECTED = "rejected"          # user rejected


LINKED_STATUSES = (KeywordLinkStatus.AUTO_LINKED, KeywordLinkStatus.CONFIRMED)
USER_DECIDED_STATUSES = (KeywordLinkStatus.CONFIRMED, KeywordLinkStatus.REJECTED)
