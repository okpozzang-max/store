"""Demand keyword <-> product matching (PRD 9.3).

Order of evidence (strongest first):
    1. GTIN token in keyword                 -> auto_linked (1.00)
    2. Model number token in keyword         -> auto_linked (0.95)
    3. Brand + product name / nickname       -> auto_linked (0.85)
    4. Brand + category term                 -> needs_review (0.60)
    5. Product name / nickname without brand -> needs_review (0.50)
    6. Brand only                            -> needs_review (0.40)

Uncertain matches are never auto-applied to scoring. When one keyword
auto-links to several products without an identifier (GTIN/model), the links
are ambiguous and are downgraded to needs_review.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from ..discovery.product_normalizer import (
    KeywordDictionary,
    contains_phrase,
    is_significant_token,
    normalize_model,
    tokenize,
)
from ..models.enums import KeywordLinkStatus
from ..models.product import Product

IDENTIFIER_METHODS = ("gtin", "model_number")


@dataclass(frozen=True)
class MatchResult:
    product_id: int
    status: KeywordLinkStatus
    method: str
    confidence: float
    reason: str


def _model_in_tokens(tokens: list[str], model_norm: str) -> bool:
    if len(model_norm) < 4:
        return False  # too short to be a reliable identifier
    candidates = [normalize_model(t) for t in tokens]
    # Single tokens and adjacent pairs ("10 10827", "heg 001").
    pairs = [a + b for a, b in zip(candidates, candidates[1:])]
    return model_norm in candidates or model_norm in pairs


def _name_terms(product: Product, dictionary: KeywordDictionary) -> list[str]:
    brand_tokens = set()
    for term in dictionary.brand_terms(product.brand):
        brand_tokens.update(term.split())
    terms = [
        t
        for t in tokenize(product.product_name)
        if t not in brand_tokens and is_significant_token(t)
    ]
    terms.extend(dictionary.nicknames(product.model_number))
    return terms


def match_keyword(
    keyword: str, product: Product, dictionary: KeywordDictionary | None = None
) -> Optional[MatchResult]:
    """Match a single keyword against a single product; None if unrelated."""
    dictionary = dictionary or KeywordDictionary()
    assert product.id is not None, "product must be persisted"
    tokens = tokenize(keyword)
    if not tokens:
        return None

    if product.gtin and product.gtin in tokens:
        return MatchResult(product.id, KeywordLinkStatus.AUTO_LINKED, "gtin", 1.0,
                           f"GTIN {product.gtin} 일치")
    model_norm = normalize_model(product.model_number)
    if model_norm and _model_in_tokens(tokens, model_norm):
        return MatchResult(product.id, KeywordLinkStatus.AUTO_LINKED, "model_number", 0.95,
                           f"모델 번호 {product.model_number} 일치")

    brand_hit = next(
        (t for t in dictionary.brand_terms(product.brand) if contains_phrase(tokens, t)), None
    )
    name_hit = next((t for t in _name_terms(product, dictionary) if contains_phrase(tokens, t)), None)
    category_hit = next(
        (t for t in dictionary.category_terms(product.category) if contains_phrase(tokens, t)), None
    )

    if brand_hit and name_hit:
        return MatchResult(product.id, KeywordLinkStatus.AUTO_LINKED, "brand_and_name", 0.85,
                           f"브랜드 '{brand_hit}' + 상품명/별칭 '{name_hit}' 일치")
    if brand_hit and category_hit:
        return MatchResult(product.id, KeywordLinkStatus.NEEDS_REVIEW, "brand_and_category", 0.60,
                           f"브랜드 '{brand_hit}' + 카테고리 '{category_hit}'만 일치 (동일 브랜드 다른 상품 가능)")
    if name_hit:
        return MatchResult(product.id, KeywordLinkStatus.NEEDS_REVIEW, "name_only", 0.50,
                           f"상품명/별칭 '{name_hit}'만 일치 (브랜드 없음)")
    if brand_hit:
        return MatchResult(product.id, KeywordLinkStatus.NEEDS_REVIEW, "brand_only", 0.40,
                           f"브랜드 '{brand_hit}'만 일치")
    return None


def match_keyword_to_products(
    keyword: str,
    products: Iterable[Product],
    dictionary: KeywordDictionary | None = None,
) -> list[MatchResult]:
    """Match a keyword against all products, resolving ambiguity."""
    results = [r for p in products if (r := match_keyword(keyword, p, dictionary)) is not None]
    soft_auto = [
        r for r in results
        if r.status == KeywordLinkStatus.AUTO_LINKED and r.method not in IDENTIFIER_METHODS
    ]
    if len(soft_auto) > 1:
        ambiguous_ids = {r.product_id for r in soft_auto}
        results = [
            MatchResult(r.product_id, KeywordLinkStatus.NEEDS_REVIEW, r.method, min(r.confidence, 0.6),
                        r.reason + f" — 여러 상품({len(ambiguous_ids)}개)과 일치하여 검토 필요")
            if r.product_id in ambiguous_ids else r
            for r in results
        ]
    return results
