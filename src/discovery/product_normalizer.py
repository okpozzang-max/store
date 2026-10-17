"""Product / keyword normalization (PRD 2.2 #4, 9.1).

Pure functions plus a user-maintained ``KeywordDictionary`` (brand aliases,
category terms, product nicknames) loaded from ``config/brand_aliases.yaml``.
No LLM output is used here: every alias comes from the user's dictionary.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

_NON_WORD = re.compile(r"[^\w\s\-.]", re.UNICODE)
_WS = re.compile(r"\s+")
_NON_ALNUM_ASCII = re.compile(r"[^0-9A-Za-z]")


def normalize_text(text: Optional[str]) -> str:
    """Lower-case, NFKC-normalize, strip punctuation and collapse whitespace.

    Hyphens and dots are kept inside tokens (``10-10827``, ``h2.0``) but
    removed when they stand alone.
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text)).lower()
    t = _NON_WORD.sub(" ", t).replace("_", " ")
    tokens = [tok.strip("-.") for tok in _WS.split(t)]
    return " ".join(tok for tok in tokens if tok)


def tokenize(text: Optional[str]) -> list[str]:
    normalized = normalize_text(text)
    return normalized.split() if normalized else []


def normalize_model(model: Optional[str]) -> str:
    """Canonical model-number form: ASCII alphanumerics only, upper-case."""
    if not model:
        return ""
    return _NON_ALNUM_ASCII.sub("", unicodedata.normalize("NFKC", str(model))).upper()


def contains_phrase(haystack_tokens: list[str], phrase: str) -> bool:
    """True if the normalized ``phrase`` appears as a whole-token sequence."""
    needle = tokenize(phrase)
    if not needle or len(needle) > len(haystack_tokens):
        return False
    n = len(needle)
    return any(haystack_tokens[i : i + n] == needle for i in range(len(haystack_tokens) - n + 1))


def is_significant_token(token: str) -> bool:
    """Tokens worth matching on: >=2 chars for Hangul, >=3 for others, not pure digits."""
    if token.isdigit():
        return False
    has_hangul = any("\uac00" <= ch <= "\ud7a3" for ch in token)
    return len(token) >= (2 if has_hangul else 3)


@dataclass
class KeywordDictionary:
    """User-maintained alias dictionary (all keys/values stored normalized)."""

    brands: dict[str, list[str]] = field(default_factory=dict)
    categories: dict[str, list[str]] = field(default_factory=dict)
    product_aliases: dict[str, list[str]] = field(default_factory=dict)  # by normalized model
    # Phase 3: local-language names for overseas search, keyed by language code.
    local_brands: dict[str, dict[str, list[str]]] = field(default_factory=dict)    # brand -> lang -> names
    local_products: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # model -> lang -> names

    @classmethod
    def from_mapping(cls, data: Optional[dict[str, Any]]) -> "KeywordDictionary":
        data = data or {}

        def _norm_map(raw: Any, key_fn) -> dict[str, list[str]]:
            out: dict[str, list[str]] = {}
            for key, values in (raw or {}).items():
                k = key_fn(str(key))
                if not k:
                    continue
                vals = values if isinstance(values, list) else [values]
                cleaned = [normalize_text(str(v)) for v in vals if normalize_text(str(v))]
                out.setdefault(k, [])
                out[k].extend(v for v in cleaned if v not in out[k])
            return out

        def _lang_map(raw: Any, key_fn) -> dict[str, dict[str, list[str]]]:
            out: dict[str, dict[str, list[str]]] = {}
            for key, by_lang in (raw or {}).items():
                k = key_fn(str(key))
                if not k or not isinstance(by_lang, dict):
                    continue
                out[k] = _norm_map(by_lang, lambda s: s.strip().lower())
            return out

        local = data.get("local_names") or {}
        return cls(
            brands=_norm_map(data.get("brands"), normalize_text),
            categories=_norm_map(data.get("categories"), normalize_text),
            product_aliases=_norm_map(data.get("product_aliases"), normalize_model),
            local_brands=_lang_map(local.get("brands"), normalize_text),
            local_products=_lang_map(local.get("products"), normalize_model),
        )

    @classmethod
    def load(cls, path: str | Path) -> "KeywordDictionary":
        p = Path(path)
        if not p.is_file():
            return cls()
        with open(p, "r", encoding="utf-8") as fh:
            return cls.from_mapping(yaml.safe_load(fh))

    def brand_terms(self, brand: Optional[str]) -> list[str]:
        """The brand itself plus its aliases, normalized."""
        base = normalize_text(brand)
        if not base:
            return []
        return [base] + [a for a in self.brands.get(base, []) if a != base]

    def brand_aliases(self, brand: Optional[str]) -> list[str]:
        return list(self.brands.get(normalize_text(brand), []))

    def category_terms(self, category: Optional[str]) -> list[str]:
        return list(self.categories.get(normalize_text(category), []))

    def nicknames(self, model_number: Optional[str]) -> list[str]:
        return list(self.product_aliases.get(normalize_model(model_number), []))

    def local_brand_names(self, brand: Optional[str], lang: str) -> list[str]:
        return list(self.local_brands.get(normalize_text(brand), {}).get(lang, []))

    def local_product_names(self, model_number: Optional[str], lang: str) -> list[str]:
        return list(self.local_products.get(normalize_model(model_number), {}).get(lang, []))

    def all_brand_terms(self, brand: Optional[str]) -> list[str]:
        """Brand, Korean aliases and every local-language name."""
        terms = self.brand_terms(brand)
        for names in self.local_brands.get(normalize_text(brand), {}).values():
            terms.extend(n for n in names if n not in terms)
        return terms

    def all_product_names(self, model_number: Optional[str]) -> list[str]:
        names = self.nicknames(model_number)
        for local in self.local_products.get(normalize_model(model_number), {}).values():
            names.extend(n for n in local if n not in names)
        return names
