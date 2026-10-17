"""Listing <-> product identity matching for collected offers (PRD 9.3).

Conservative by design: only identifier evidence (model number / GTIN query)
without red flags is accepted automatically, and even then the offer stays
``unverified`` until the user confirms it. Brand + name evidence only produces
``needs_review`` candidates, which are excluded from price comparison.
Listings with no identifier and no name evidence are discarded.

Works for space-less CJK titles: non-ASCII terms are matched as substrings of
the compacted title, ASCII terms as whole tokens.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from ..discovery.product_normalizer import (
    KeywordDictionary,
    contains_phrase,
    is_significant_token,
    normalize_model,
    normalize_text,
    tokenize,
)
from ..models.enums import VerificationStatus
from ..models.product import Product

_ASCII_TOKEN = re.compile(r"[0-9a-z]+")

# Words indicating the listing is not the product itself (accessory, part,
# compatible item), a second-hand item, or a multi-unit bundle.
ACCESSORY_MARKERS = (
    "case", "cover", "skin", "protector", "compatible", "replacement", "parts", "part",
    "adapter", "attachment", "holder", "stand", "sticker", "decal", "manual", "box only",
    "for ", "ケース", "カバー", "対応", "互換", "交換", "部品", "フィルム", "専用",
    "케이스", "커버", "호환", "부품", "필름", "거치대", "교체",
)
USED_MARKERS = ("used", "pre-owned", "preowned", "refurbished", "中古", "중고", "리퍼")
BUNDLE_MARKERS = ("lot of", "bundle", "まとめ売り", "묶음")


@dataclass(frozen=True)
class OfferMatch:
    status: VerificationStatus
    confidence: float
    method: str
    reason: str
    red_flags: tuple[str, ...] = field(default_factory=tuple)


def _clean(title: str) -> str:
    t = unicodedata.normalize("NFKC", title or "")
    t = re.sub(r"<[^>]+>", " ", t)  # Naver wraps matches in <b>
    return t


def _is_ascii(s: str) -> bool:
    return all(ord(c) < 128 for c in s)


def _term_in(term: str, tokens: list[str], compact: str) -> bool:
    if not term:
        return False
    if _is_ascii(term):
        return contains_phrase(tokens, term)
    return term.replace(" ", "") in compact


def _marker_hits(markers: tuple[str, ...], lowered: str, tokens: list[str], compact: str) -> list[str]:
    hits = []
    for m in markers:
        if _is_ascii(m):
            if m.endswith(" "):  # "for " -> token "for"
                if m.strip() in tokens:
                    hits.append(m.strip())
            elif " " in m:
                if m in lowered:
                    hits.append(m)
            elif m in tokens:
                hits.append(m)
        elif m in compact:
            hits.append(m)
    return hits


def _model_hit(title_lower: str, model: Optional[str]) -> bool:
    target = normalize_model(model)
    if len(target) < 4:
        return False
    toks = [t.upper() for t in _ASCII_TOKEN.findall(title_lower)]
    for n in (1, 2, 3):
        for i in range(len(toks) - n + 1):
            if "".join(toks[i : i + n]) == target:
                return True
    return False


def match_listing(
    title: str,
    product: Product,
    dictionary: Optional[KeywordDictionary] = None,
    query_kind: str = "keyword",
    condition: Optional[str] = None,
) -> Optional[OfferMatch]:
    """Classify a listing title against a product; None if unrelated."""
    d = dictionary or KeywordDictionary()
    cleaned = _clean(title)
    lowered = cleaned.lower()
    tokens = tokenize(cleaned)
    compact = normalize_text(cleaned).replace(" ", "")
    if not tokens:
        return None

    brand_terms = d.all_brand_terms(product.brand)
    brand_hit = next((b for b in brand_terms if _term_in(b, tokens, compact)), None)
    model_hit = _model_hit(lowered, product.model_number)
    model_norm = normalize_model(product.model_number)
    model_is_numeric = model_norm.isdigit()

    brand_token_set = {t for b in brand_terms for t in b.split()}
    name_tokens = [
        t for t in tokenize(product.product_name)
        if t not in brand_token_set and is_significant_token(t)
    ]
    name_hits = [t for t in name_tokens if t in tokens]
    nick_hit = next((n for n in d.all_product_names(product.model_number) if _term_in(n, tokens, compact)), None)

    red: list[str] = []
    acc = _marker_hits(ACCESSORY_MARKERS, lowered, tokens, compact)
    # Words that are part of the product's own name are not accessory markers.
    acc = [a for a in acc if a not in tokenize(product.product_name)]
    if acc:
        red.append("액세서리/호환품 의심(" + ", ".join(acc[:3]) + ")")
    used = _marker_hits(USED_MARKERS, lowered, tokens, compact)
    if used or _marker_hits(USED_MARKERS, (condition or "").lower(), tokenize(condition), normalize_text(condition).replace(" ", "")):
        red.append("중고/리퍼 의심")
    bundle = _marker_hits(BUNDLE_MARKERS, lowered, tokens, compact)
    if bundle:
        red.append("묶음/다수량 의심")

    variant_note = ""
    if product.variant:
        vt = [
            part
            for t in tokenize(product.variant)
            for part in re.split(r"[-/.]", t)
            if part and is_significant_token(part)
        ]
        if vt and not all(_term_in(t, tokens, compact) for t in vt):
            variant_note = f" · 옵션 '{product.variant}' 제목에서 미확인"

    if query_kind == "gtin":
        if brand_hit and not red:
            return OfferMatch(VerificationStatus.UNVERIFIED, 0.9, "gtin_query",
                              f"GTIN 검색 결과 + 브랜드 '{brand_hit}' 일치{variant_note}")
        return OfferMatch(VerificationStatus.NEEDS_REVIEW, 0.6, "gtin_query",
                          "GTIN 검색 결과이나 제목 근거 부족" + (" · " + "; ".join(red) if red else ""),
                          tuple(red))

    if model_hit:
        strong = brand_hit is not None or not model_is_numeric
        if strong and not red:
            conf = 0.9 if not variant_note else 0.8
            reason = f"모델 번호 {product.model_number} 일치" + (f" + 브랜드 '{brand_hit}'" if brand_hit else "")
            return OfferMatch(VerificationStatus.UNVERIFIED, conf, "model_number", reason + variant_note)
        why = []
        if not strong:
            why.append("숫자 모델번호만 일치(브랜드 없음)")
        why.extend(red)
        return OfferMatch(VerificationStatus.NEEDS_REVIEW, 0.5, "model_number",
                          f"모델 번호 일치하나 확인 필요: {'; '.join(why)}{variant_note}", tuple(red))

    enough_name = bool(name_tokens) and len(name_hits) / len(name_tokens) >= 0.5
    if brand_hit and (enough_name or nick_hit):
        evidence = f"상품명 {len(name_hits)}/{len(name_tokens)} 단어" if enough_name else f"별칭 '{nick_hit}'"
        return OfferMatch(VerificationStatus.NEEDS_REVIEW, 0.6, "brand_and_name",
                          f"브랜드 '{brand_hit}' + {evidence} 일치 (모델 번호 없음 — 확인 필요)"
                          + (" · " + "; ".join(red) if red else "") + variant_note, tuple(red))
    return None
