"""Cost assumptions: forwarding (international) shipping, customs, channel fees,
import-risk rules (PRD 8.1, 8.2, 8.4).

Everything here is an ESTIMATE driven by config/scoring_config.yaml and
config/import_rules.yaml. Each estimate returns a human-readable basis string
so the UI can show exactly what was assumed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from ..discovery.product_normalizer import normalize_text
from ..models.product import Product
from ..utils.currency import CurrencyConverter, UnsupportedCurrencyError

EU_COUNTRIES = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT",
    "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
}


@dataclass(frozen=True)
class Channel:
    key: str
    label: str
    fee_pct: float


@dataclass(frozen=True)
class Estimate:
    value_krw: float
    estimated: bool
    basis: str


@dataclass
class CostAssumptions:
    overseas_card_fee_pct: float = 0.025
    fx_buffer_pct: float = 0.03
    return_allowance_pct: float = 0.03
    packaging_krw: float = 1000.0
    min_weight_kg: float = 1.0
    rate_krw_per_kg: dict[str, float] = field(default_factory=lambda: {"default": 15000.0})
    default_weight_by_category: dict[str, float] = field(default_factory=dict)
    default_weight_kg: float = 2.0
    duty_free_usd_general: float = 150.0
    duty_free_usd_us: float = 200.0
    vat_pct: float = 0.10
    default_duty_rate: float = 0.08
    duty_rate_by_category: dict[str, float] = field(default_factory=dict)
    channels: dict[str, Channel] = field(default_factory=dict)
    default_channel: str = "smartstore"
    max_lead_time_days: int = 14

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "CostAssumptions":
        c = config.get("costs", {}) or {}
        fw = c.get("forwarding", {}) or {}
        cu = c.get("customs", {}) or {}
        ch = dict(config.get("sales_channels", {}) or {})
        default_channel = str(ch.pop("default", "smartstore"))
        channels = {
            k: Channel(k, str(v.get("label", k)), float(v.get("fee_pct", 0.10)))
            for k, v in ch.items() if isinstance(v, dict)
        } or {"other": Channel("other", "기타", 0.10)}
        if default_channel not in channels:
            default_channel = next(iter(channels))
        norm = lambda d: {normalize_text(k): float(v) for k, v in (d or {}).items()}  # noqa: E731
        return cls(
            overseas_card_fee_pct=float(c.get("overseas_card_fee_pct", 0.025)),
            fx_buffer_pct=float(c.get("fx_buffer_pct", 0.03)),
            return_allowance_pct=float(c.get("return_allowance_pct", 0.03)),
            packaging_krw=float(c.get("packaging_krw", 1000)),
            min_weight_kg=float(fw.get("min_weight_kg", 1.0)),
            rate_krw_per_kg={str(k).upper(): float(v) for k, v in (fw.get("rate_krw_per_kg") or {"default": 15000}).items()},
            default_weight_by_category=norm(fw.get("default_weight_kg_by_category")),
            default_weight_kg=float(fw.get("default_weight_kg", 2.0)),
            duty_free_usd_general=float(cu.get("duty_free_usd_general", 150)),
            duty_free_usd_us=float(cu.get("duty_free_usd_us", 200)),
            vat_pct=float(cu.get("vat_pct", 0.10)),
            default_duty_rate=float(cu.get("default_duty_rate", 0.08)),
            duty_rate_by_category=norm(cu.get("duty_rate_by_category")),
            channels=channels,
            default_channel=default_channel,
            max_lead_time_days=int((config.get("business", {}) or {}).get("max_lead_time_days", 14)),
        )

    def channel(self, key: Optional[str]) -> Channel:
        return self.channels.get(key or "", self.channels[self.default_channel])

    # ------------------------------------------------------------ estimates

    def weight(self, product: Product) -> tuple[float, bool, str]:
        if product.weight_kg:
            return product.weight_kg, False, f"상품 무게 {product.weight_kg}kg"
        cat = normalize_text(product.category)
        if cat in self.default_weight_by_category:
            w = self.default_weight_by_category[cat]
            return w, True, f"카테고리 '{product.category}' 기본 무게 {w}kg(추정)"
        return self.default_weight_kg, True, f"기본 무게 {self.default_weight_kg}kg(추정)"

    def international_shipping(self, product: Product, supplier_country: Optional[str]) -> Estimate:
        w, w_est, w_basis = self.weight(product)
        country = (supplier_country or "").upper()
        key = "EU" if country in EU_COUNTRIES and country not in self.rate_krw_per_kg else country
        rate = self.rate_krw_per_kg.get(key, self.rate_krw_per_kg.get("DEFAULT", 15000.0))
        billable = max(w, self.min_weight_kg)
        return Estimate(
            billable * rate, True,
            f"배송대행 {billable:g}kg × {rate:,.0f}원/kg ({key or '기본'} 요율, {w_basis})",
        )

    def customs(
        self, product: Product, supplier_country: Optional[str], overseas_payment_krw: float,
        intl_shipping_krw: float, converter: CurrencyConverter,
    ) -> Estimate:
        """Duty + VAT estimate for a personal import.

        Duty-free judged on item price incl. seller shipping (USD). Above the
        threshold: duty = (payment + int'l shipping) x rate, VAT = (value + duty) x vat.
        """
        us = (supplier_country or "").upper() == "US"
        limit = self.duty_free_usd_us if us else self.duty_free_usd_general
        try:
            usd = overseas_payment_krw / converter.get_rate("USD").rate_to_krw
        except UnsupportedCurrencyError:
            usd = None
        if usd is not None and usd <= limit:
            return Estimate(0.0, True, f"물품가 약 US${usd:,.0f} ≤ 면세 기준 US${limit:g} → 0원(추정)")
        rate = self.duty_rate_by_category.get(normalize_text(product.category), self.default_duty_rate)
        value = overseas_payment_krw + intl_shipping_krw
        duty = value * rate
        vat = (value + duty) * self.vat_pct
        head = f"물품가 약 US${usd:,.0f} > 면세 기준 US${limit:g}" if usd is not None else "USD 환율 없음 → 과세로 가정"
        return Estimate(
            duty + vat, True,
            f"{head}: 관세 {rate:.0%} {duty:,.0f}원 + 부가세 {self.vat_pct:.0%} {vat:,.0f}원(추정, 품목별 세율 확인 필요)",
        )


# ---------------------------------------------------------------- import rules


@dataclass(frozen=True)
class ImportRule:
    tag: str
    label: str
    keywords: tuple[str, ...]
    categories: tuple[str, ...]


def load_import_rules(path: str | Path) -> list[ImportRule]:
    p = Path(path)
    if not p.is_file():
        return []
    with open(p, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return [
        ImportRule(
            tag=str(r["tag"]),
            label=str(r.get("label", r["tag"])),
            keywords=tuple(normalize_text(str(k)) for k in r.get("keywords") or []),
            categories=tuple(normalize_text(str(c)) for c in r.get("categories") or []),
        )
        for r in data.get("rules") or [] if r.get("tag")
    ]


def match_import_rules(product: Product, rules: list[ImportRule]) -> list[ImportRule]:
    text = " " + normalize_text(" ".join(filter(None, [product.brand, product.product_name, product.variant]))) + " "
    cat = normalize_text(product.category)
    hits = []
    for r in rules:
        if cat and cat in r.categories:
            hits.append(r)
        elif any(k and (f" {k} " in text or (not k.isascii() and k in text)) for k in r.keywords):
            hits.append(r)
    return hits
