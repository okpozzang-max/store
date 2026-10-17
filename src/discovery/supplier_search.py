"""Staged, multilingual search plans for overseas supplier search (PRD 9.2).

Steps, most to least specific:
    1. exact model number (eBay additionally tries the GTIN parameter first)
    2. brand + model number (incl. local-language brand names)
    3. brand + product name / nickname (in the source's language)
    4. local-language product name
    5. brand + category term (broad; results usually need manual review)

All local-language terms come from the user's dictionary (config/brand_aliases.yaml);
nothing is machine-translated or LLM-generated.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models.product import Product
from .product_normalizer import KeywordDictionary, normalize_text

STEP_LABELS = {
    0: "GTIN",
    1: "모델 번호",
    2: "브랜드 + 모델 번호",
    3: "브랜드 + 상품명",
    4: "현지어 상품명",
    5: "브랜드 + 카테고리",
}


@dataclass(frozen=True)
class SearchQuery:
    step: int
    text: str
    lang: str
    kind: str = "keyword"  # "keyword" | "gtin"

    @property
    def label(self) -> str:
        return STEP_LABELS.get(self.step, str(self.step))


def build_search_plan(
    product: Product,
    dictionary: KeywordDictionary | None,
    lang: str,
    supports_gtin: bool = False,
) -> list[SearchQuery]:
    """Ordered, de-duplicated queries for one product in one language."""
    d = dictionary or KeywordDictionary()
    brand = (product.brand or "").strip()
    model = (product.model_number or "").strip()
    name = product.product_name.strip()

    if lang == "ko":
        brand_names = d.brand_aliases(brand)
        product_names = d.nicknames(model)
        categories = d.category_terms(product.category)
    elif lang == "en":
        brand_names = []
        product_names = d.local_product_names(model, "en")
        categories = d.local_product_names(product.category, "en")  # usually empty
    else:
        brand_names = d.local_brand_names(brand, lang)
        product_names = d.local_product_names(model, lang)
        categories = []

    raw: list[tuple[int, str, str]] = []
    if supports_gtin and product.gtin:
        raw.append((0, product.gtin, "gtin"))
    if model:
        raw.append((1, model, "keyword"))
        for b in ([brand] if brand else []) + brand_names:
            raw.append((2, f"{b} {model}", "keyword"))
    if lang == "en":
        has_brand = brand and normalize_text(name).startswith(normalize_text(brand))
        raw.append((3, name if (has_brand or not brand) else f"{brand} {name}", "keyword"))
    for b in brand_names:
        for p in product_names:
            raw.append((3, f"{b} {p}", "keyword"))
    for p in product_names:
        raw.append((4, p, "keyword"))
    for b in brand_names or ([brand] if brand else []):
        for c in categories:
            raw.append((5, f"{b} {c}", "keyword"))

    seen: set[tuple[str, str]] = set()
    plan: list[SearchQuery] = []
    for step, text, kind in raw:
        key = (kind, normalize_text(text))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        plan.append(SearchQuery(step=step, text=text, lang=lang, kind=kind))
    return plan
