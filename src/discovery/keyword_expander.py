"""Rule-based keyword candidate generation (PRD 9.1, 9.2).

Generates search-query candidates for a product from its own attributes and
the user's dictionary, ordered from most to least specific (PRD 9.2):
model number -> brand + model -> brand + product name -> Korean brand +
nickname -> Korean brand + category term.

Candidates are *query terms* to collect demand for. They are stored with
status CANDIDATE and never count as a demand link until an observed demand
keyword matches the product (see matching.product_matcher).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models.product import Product
from .product_normalizer import KeywordDictionary, normalize_text

MAX_CANDIDATES_PER_PRODUCT = 12


@dataclass(frozen=True)
class KeywordCandidate:
    keyword: str
    normalized: str
    reason: str


def generate_keyword_candidates(
    product: Product,
    dictionary: KeywordDictionary | None = None,
    limit: int = MAX_CANDIDATES_PER_PRODUCT,
) -> list[KeywordCandidate]:
    dictionary = dictionary or KeywordDictionary()
    brand = (product.brand or "").strip()
    model = (product.model_number or "").strip()
    name = product.product_name.strip()

    raw: list[tuple[str, str]] = []
    if model:
        raw.append((model, "모델 번호"))
        if brand:
            raw.append((f"{brand} {model}", "브랜드 + 모델 번호"))
    if brand and normalize_text(name).startswith(normalize_text(brand)):
        raw.append((name, "브랜드 + 상품명"))
    elif brand:
        raw.append((f"{brand} {name}", "브랜드 + 상품명"))
    else:
        raw.append((name, "상품명"))

    aliases = dictionary.brand_aliases(brand)
    for alias in aliases:
        for nick in dictionary.nicknames(model):
            raw.append((f"{alias} {nick}", "한글 브랜드 + 상품 별칭 (사전)"))
    for alias in aliases:
        for term in dictionary.category_terms(product.category):
            raw.append((f"{alias} {term}", "한글 브랜드 + 카테고리 (사전, 광범위)"))

    seen: set[str] = set()
    out: list[KeywordCandidate] = []
    for keyword, reason in raw:
        norm = normalize_text(keyword)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(KeywordCandidate(keyword=keyword, normalized=norm, reason=reason))
        if len(out) >= limit:
            break
    return out
