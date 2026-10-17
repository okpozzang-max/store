"""Supplier trust assessment (PRD 9.4).

Produces a 0..100 trust score, human-readable flags and an exclusion decision
for one offer. The lowest price alone never makes an offer the recommended one:
offers that are out of stock, used, from a blocked supplier, or below the
minimum trust score are excluded before price comparison.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..models.enums import AvailabilityStatus, SupplierType, TrustLevel
from ..models.supplier import Supplier, SupplierOffer

BASE_SCORE = {
    SupplierType.OFFICIAL_STORE: 85,
    SupplierType.AUTHORIZED_RETAILER: 75,
    SupplierType.SPECIALTY_RETAILER: 65,
    SupplierType.WHOLESALE: 55,
    SupplierType.MARKETPLACE_SELLER: 50,
    SupplierType.PRICE_COMPARISON: 45,
    SupplierType.UNKNOWN: 50,
}
MIN_RATING_COUNT = 10
USED_MARKERS = ("used", "pre-owned", "preowned", "refurbished", "中古", "중고", "리퍼")


@dataclass
class TrustAssessment:
    score: float
    excluded: bool
    exclusion_reason: Optional[str] = None
    flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "score": self.score,
            "excluded": self.excluded,
            "exclusion_reason": self.exclusion_reason,
            "flags": self.flags,
            "reasons": self.reasons,
        }


def is_used_condition(condition: Optional[str]) -> bool:
    c = (condition or "").lower()
    return any(m in c for m in USED_MARKERS)


def assess_offer_trust(
    offer: SupplierOffer,
    supplier: Optional[Supplier] = None,
    min_score: float = 40.0,
) -> TrustAssessment:
    stype = offer.supplier_type
    if stype == SupplierType.UNKNOWN and supplier is not None:
        stype = supplier.supplier_type
    score = float(BASE_SCORE[stype])
    reasons = [f"공급처 유형 {stype.value} 기본 {score:.0f}점"]
    flags: list[str] = []
    excluded_reason: Optional[str] = None

    if supplier is not None and supplier.trust_level == TrustLevel.BLOCKED:
        excluded_reason = "사용자가 차단한 공급처"
    elif supplier is not None and supplier.trust_level == TrustLevel.TRUSTED:
        score += 15
        reasons.append("사용자 신뢰 공급처 +15")

    if offer.availability_status == AvailabilityStatus.OUT_OF_STOCK:
        excluded_reason = excluded_reason or "품절"
    elif offer.availability_status == AvailabilityStatus.UNKNOWN:
        score -= 5
        flags.append("재고 미확인")

    if is_used_condition(offer.item_condition):
        excluded_reason = excluded_reason or f"중고/리퍼 상품 ({offer.item_condition})"

    if offer.seller_rating is not None:
        count = offer.seller_rating_count or 0
        r = offer.seller_rating
        if count < MIN_RATING_COUNT:
            flags.append(f"평가 수 부족({count})")
            if r < 90:
                score -= 10
        else:
            adj = 10 if r >= 98 else 5 if r >= 95 else 0 if r >= 90 else -10 if r >= 80 else -25
            score += adj
            reasons.append(f"평가 {r:.1f} ({count}건) {adj:+d}")
    else:
        flags.append("판매자 평가 정보 없음")

    ships = offer.ships_to_kr
    if ships is None and supplier is not None:
        ships = supplier.ships_to_kr
    if ships is False:
        score -= 10
        flags.append("한국 직배송 불가(배송대행 필요)")
    elif ships is None:
        score -= 5
        flags.append("한국 배송 여부 미확인")

    if not offer.shipping_cost_known:
        score -= 5
        flags.append("배송비 미확인")

    if supplier is not None:
        if supplier.purchase_restrictions:
            flags.append(f"구매 제한: {supplier.purchase_restrictions}")
        if supplier.authenticity_notes:
            flags.append(f"정품/IP 메모: {supplier.authenticity_notes}")
        if not supplier.return_policy:
            flags.append("반품 정책 미확인")

    score = max(0.0, min(100.0, score))
    if excluded_reason is None and score < min_score:
        excluded_reason = f"신뢰도 {score:.0f} < 최소 {min_score:.0f}"
    return TrustAssessment(
        score=round(score, 1),
        excluded=excluded_reason is not None,
        exclusion_reason=excluded_reason,
        flags=flags,
        reasons=reasons,
    )
