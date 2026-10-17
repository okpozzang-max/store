"""CSV collectors (PRD 2.2 #10, 4, 14 #6).

Reads demand signals, products, supplier offers and domestic offers from CSV
files using only the standard library. Each row is validated via its Pydantic
model; a bad row is captured as an error and does not abort the whole import.

Expected CSV columns (header row required):

demand_signals.csv:
    keyword, category, country, observed_at, period_start, period_end,
    demand_index, growth_pct, signal_type, confidence, raw_reference

products.csv:
    brand, product_name, model_number, gtin, category, variant,
    country_of_origin[, weight_kg, my_sale_price_krw]

supplier_offers.csv:
    product_ref, supplier_name, supplier_country, product_url, foreign_price,
    currency, availability_status, shipping_cost, shipping_currency,
    price_checked_at, match_confidence, verification_status[, lead_time_days]

domestic_offers.csv:
    product_ref, seller_name, product_url, price_krw, shipping_cost_krw,
    product_condition, match_confidence, price_checked_at[, verification_status]

``product_ref`` links an offer to a product by product_name (resolved by the
pipeline) and is kept out of the typed model until resolution.
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..models.demand import DemandSignal
from ..models.product import Product
from ..models.supplier import DomesticOffer, SupplierOffer
from .base import BaseCollector, CollectionResult


def _s(value: Optional[str]) -> Optional[str]:
    """Normalize a CSV string cell: strip, empty -> None."""
    if value is None:
        return None
    v = value.strip()
    return v if v else None


def _f(value: Optional[str]) -> Optional[float]:
    v = _s(value)
    if v is None:
        return None
    return float(v)


def _dt(value: Optional[str]) -> Optional[datetime]:
    v = _s(value)
    if v is None:
        return None
    return datetime.fromisoformat(v)


class CsvCollector(BaseCollector[dict]):
    """Generic CSV reader returning raw dict rows with error capture."""

    source_name = "csv"

    def is_supported(self, target: Any) -> bool:
        try:
            path = Path(target)
        except TypeError:
            return False
        return path.suffix.lower() == ".csv" and path.is_file()

    def collect(self, target: Any) -> CollectionResult[dict]:
        path = Path(target)
        result: CollectionResult[dict] = CollectionResult(
            source_name=self.source_name, reference=str(path)
        )
        if not self.is_supported(target):
            result.errors.append(f"Not a readable CSV file: {path}")
            return result
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for i, row in enumerate(reader, start=2):  # header is line 1
                try:
                    result.records.append({k: _s(v) for k, v in row.items()})
                except Exception as exc:  # noqa: BLE001 - row-level resilience
                    result.errors.append(f"row {i}: {exc}")
        return result


class DemandSignalCsvCollector(BaseCollector[DemandSignal]):
    source_name = "csv:demand_signals"

    def is_supported(self, target: Any) -> bool:
        return CsvCollector().is_supported(target)

    def collect(self, target: Any) -> CollectionResult[DemandSignal]:
        raw = CsvCollector().collect(target)
        result: CollectionResult[DemandSignal] = CollectionResult(
            source_name=self.source_name, reference=raw.reference, errors=list(raw.errors)
        )
        for i, row in enumerate(raw.records, start=2):
            try:
                signal = DemandSignal(
                    keyword=row.get("keyword") or "",
                    category=row.get("category"),
                    country=row.get("country"),
                    observed_at=_dt(row.get("observed_at")),
                    period_start=_dt(row.get("period_start")),
                    period_end=_dt(row.get("period_end")),
                    demand_index=_f(row.get("demand_index")) or 0.0,
                    growth_pct=_f(row.get("growth_pct")),
                    signal_type=(row.get("signal_type") or "search_interest"),
                    confidence=(row.get("confidence") or "unverified"),
                    raw_reference=row.get("raw_reference"),
                )
                result.records.append(signal)
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"demand row {i}: {exc}")
        return result


class ProductCsvCollector(BaseCollector[Product]):
    source_name = "csv:products"

    def is_supported(self, target: Any) -> bool:
        return CsvCollector().is_supported(target)

    def collect(self, target: Any) -> CollectionResult[Product]:
        raw = CsvCollector().collect(target)
        result: CollectionResult[Product] = CollectionResult(
            source_name=self.source_name, reference=raw.reference, errors=list(raw.errors)
        )
        for i, row in enumerate(raw.records, start=2):
            try:
                product = Product(
                    brand=row.get("brand"),
                    product_name=row.get("product_name") or "",
                    model_number=row.get("model_number"),
                    gtin=row.get("gtin"),
                    category=row.get("category"),
                    variant=row.get("variant"),
                    country_of_origin=row.get("country_of_origin"),
                    weight_kg=_f(row.get("weight_kg")),
                    my_sale_price_krw=_f(row.get("my_sale_price_krw")),
                )
                result.records.append(product)
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"product row {i}: {exc}")
        return result


class SupplierOfferCsvCollector(BaseCollector[tuple]):
    """Returns (product_ref, SupplierOffer) tuples; product_id resolved later."""

    source_name = "csv:supplier_offers"

    def is_supported(self, target: Any) -> bool:
        return CsvCollector().is_supported(target)

    def collect(self, target: Any) -> CollectionResult[tuple]:
        raw = CsvCollector().collect(target)
        result: CollectionResult[tuple] = CollectionResult(
            source_name=self.source_name, reference=raw.reference, errors=list(raw.errors)
        )
        for i, row in enumerate(raw.records, start=2):
            try:
                product_ref = row.get("product_ref") or ""
                offer = SupplierOffer(
                    product_id=0,  # placeholder; resolved by pipeline
                    supplier_name=row.get("supplier_name") or "",
                    supplier_country=row.get("supplier_country"),
                    product_url=row.get("product_url"),
                    foreign_price=_f(row.get("foreign_price")) or 0.0,
                    currency=row.get("currency") or "",
                    availability_status=(row.get("availability_status") or "unknown"),
                    shipping_cost=_f(row.get("shipping_cost")) or 0.0,
                    shipping_currency=row.get("shipping_currency"),
                    price_checked_at=_dt(row.get("price_checked_at")),
                    match_confidence=_f(row.get("match_confidence")) or 0.0,
                    verification_status=(row.get("verification_status") or "unverified"),
                    lead_time_days=int(_f(row.get("lead_time_days"))) if _f(row.get("lead_time_days")) is not None else None,
                )
                result.records.append((product_ref, offer))
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"supplier offer row {i}: {exc}")
        return result


class DomesticOfferCsvCollector(BaseCollector[tuple]):
    """Returns (product_ref, DomesticOffer) tuples; product_id resolved later."""

    source_name = "csv:domestic_offers"

    def is_supported(self, target: Any) -> bool:
        return CsvCollector().is_supported(target)

    def collect(self, target: Any) -> CollectionResult[tuple]:
        raw = CsvCollector().collect(target)
        result: CollectionResult[tuple] = CollectionResult(
            source_name=self.source_name, reference=raw.reference, errors=list(raw.errors)
        )
        for i, row in enumerate(raw.records, start=2):
            try:
                product_ref = row.get("product_ref") or ""
                offer = DomesticOffer(
                    product_id=0,  # placeholder; resolved by pipeline
                    seller_name=row.get("seller_name") or "",
                    product_url=row.get("product_url"),
                    price_krw=_f(row.get("price_krw")) or 0.0,
                    shipping_cost_krw=_f(row.get("shipping_cost_krw")) or 0.0,
                    product_condition=row.get("product_condition"),
                    match_confidence=_f(row.get("match_confidence")) or 0.0,
                    price_checked_at=_dt(row.get("price_checked_at")),
                    verification_status=(row.get("verification_status") or "unverified"),
                )
                result.records.append((product_ref, offer))
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"domestic offer row {i}: {exc}")
        return result
