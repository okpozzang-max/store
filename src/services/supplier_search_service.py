"""Overseas supplier / domestic price search service (PRD 3-B, 4.4, 9.2-9.4).

For each configured source and each product:
    1. build a staged, language-specific search plan (most specific first)
    2. run queries until the call budget is used or a strong match is found
    3. match every listing to the product (identifier evidence only is
       auto-accepted; uncertain listings become needs_review; unrelated
       listings are discarded)
    4. store observations (price history preserved, user decisions kept)

Sources without credentials or disabled by the user are SKIPPED and recorded;
one failing source never stops the others, and failures never touch existing data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from ..collectors.http_client import ApiAuthError, ApiError
from ..collectors.supplier_sources import EbayBrowseSource, Listing, OfferSource, RakutenIchibaSource
from ..discovery.supplier_search import build_search_plan
from ..matching.offer_matcher import OfferMatch, match_listing
from ..models.demand import DataSource
from ..models.enums import RunStatus, VerificationStatus
from ..models.product import Product
from ..models.supplier import DomesticOffer, SupplierOffer
from ..utils.logging_config import get_logger
from .pipeline import AnalysisPipeline

logger = get_logger(__name__)


@dataclass
class SourceSpec:
    """A source the user can enable; ``factory`` is None without credentials."""

    source_name: str
    country: str
    is_domestic: bool
    terms_url: str
    license_notes: str
    factory: Optional[Callable[[], OfferSource]]
    missing_reason: str = ""

    @classmethod
    def for_class(cls, klass, country: str, factory, missing_reason: str) -> "SourceSpec":
        return cls(klass.source_name, country, klass.is_domestic, klass.terms_url,
                   klass.license_notes, factory, missing_reason)


@dataclass
class SearchOutcome:
    source_name: str
    status: RunStatus
    products_searched: int = 0
    queries: int = 0
    listings_seen: int = 0
    matched_auto: int = 0
    matched_review: int = 0
    discarded: int = 0
    new_observations: int = 0
    price_changes: int = 0
    refreshed: int = 0
    calls_made: int = 0
    errors: list[str] = field(default_factory=list)
    message: str = ""


class SupplierSearchService:
    def __init__(
        self,
        pipeline: AnalysisPipeline,
        specs: list[SourceSpec],
        max_queries_per_product: int = 3,
        stop_on_strong_match: bool = True,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.pipeline = pipeline
        self.specs = specs
        self.max_queries = max(1, max_queries_per_product)
        self.stop_on_strong_match = stop_on_strong_match
        self._now = now
        self.source_ids: dict[str, int] = {}
        for spec in specs:
            self.source_ids[spec.source_name] = pipeline.sources.ensure(
                DataSource(
                    name=spec.source_name,
                    source_type="domestic_price" if spec.is_domestic else "supplier_search",
                    country=spec.country,
                    access_method="api",
                    terms_url=spec.terms_url,
                    license_notes=spec.license_notes,
                )
            )

    # ------------------------------------------------------------------ helpers

    def _skip(self, source_id: int, name: str, reason: str) -> SearchOutcome:
        run_id = self.pipeline.runs.start(source_id)
        self.pipeline.runs.finish(run_id, RunStatus.SKIPPED, error_summary=reason)
        return SearchOutcome(name, RunStatus.SKIPPED, message=reason)

    def _store(
        self, source_id: int, spec: SourceSpec, product: Product, listing: Listing,
        match: OfferMatch, query: str,
    ) -> str:
        assert product.id is not None
        now = self._now()
        if spec.is_domestic:
            offer = DomesticOffer(
                product_id=product.id,
                seller_name=listing.seller_name,
                product_url=listing.url,
                price_krw=listing.price,
                shipping_cost_krw=listing.shipping_cost or 0.0,
                shipping_cost_known=listing.shipping_cost is not None,
                product_condition="new",
                match_confidence=match.confidence,
                price_checked_at=now,
                verification_status=match.status,
                source_id=source_id,
                listing_title=listing.title[:300],
                external_id=listing.external_id,
                match_method=match.method,
                match_reason=match.reason,
            )
            _, kind = self.pipeline.domestic_offers.record_observation(offer)
            return kind
        self.pipeline.suppliers.ensure(listing.seller_name, listing.supplier_country, listing.supplier_type)
        offer = SupplierOffer(
            product_id=product.id,
            source_id=source_id,
            supplier_name=listing.seller_name,
            supplier_country=listing.supplier_country,
            product_url=listing.url,
            foreign_price=listing.price,
            currency=listing.currency,
            availability_status=listing.availability,
            shipping_cost=listing.shipping_cost or 0.0,
            shipping_currency=listing.shipping_currency or listing.currency,
            shipping_cost_known=listing.shipping_cost is not None,
            price_checked_at=now,
            match_confidence=match.confidence,
            verification_status=match.status,
            supplier_type=listing.supplier_type,
            listing_title=listing.title[:300],
            external_id=listing.external_id,
            item_condition=listing.condition,
            ships_to_kr=listing.ships_to_kr,
            seller_rating=listing.seller_rating,
            seller_rating_count=listing.seller_rating_count,
            match_method=match.method,
            match_reason=match.reason + (f" · {listing.notes}" if listing.notes else ""),
            search_query=query,
        )
        _, kind = self.pipeline.supplier_offers.record_observation(offer)
        return kind

    # ------------------------------------------------------------------ run

    def search_source(self, spec: SourceSpec, products: list[Product]) -> SearchOutcome:
        source_id = self.source_ids[spec.source_name]
        source_row = self.pipeline.sources.get(source_id)
        if source_row is not None and not source_row.enabled:
            return self._skip(source_id, spec.source_name, "데이터 소스가 비활성화되어 있어 건너뜀")
        if spec.factory is None:
            return self._skip(source_id, spec.source_name, spec.missing_reason)

        source = spec.factory()
        run_id = self.pipeline.runs.start(source_id)
        out = SearchOutcome(spec.source_name, RunStatus.RUNNING)
        stop = False
        for product in products:
            if stop:
                break
            plan = build_search_plan(product, self.pipeline.dictionary, source.lang, source.supports_gtin)
            if not plan:
                continue
            out.products_searched += 1
            seen_urls: set[str] = set()
            for query in plan[: self.max_queries]:
                if source.client.calls_remaining <= 0:
                    out.errors.append(f"호출 상한({source.client.max_calls}회) 도달 — 이후 상품 미탐색")
                    stop = True
                    break
                try:
                    listings = source.search(query.text, query.kind)
                except ApiAuthError as exc:
                    out.errors.append(f"인증/권한 오류로 중단: {exc}")
                    stop = True
                    break
                except ApiError as exc:
                    out.errors.append(f"'{product.product_name}' / '{query.text}': {exc}")
                    continue
                out.queries += 1
                strong = False
                for listing in listings:
                    key = listing.url or f"{listing.seller_name}|{listing.title}"
                    if key in seen_urls:
                        continue
                    seen_urls.add(key)
                    out.listings_seen += 1
                    match = match_listing(listing.title, product, self.pipeline.dictionary,
                                          query.kind, listing.condition)
                    if match is None:
                        out.discarded += 1
                        continue
                    if match.status == VerificationStatus.UNVERIFIED:
                        out.matched_auto += 1
                        strong = True
                    else:
                        out.matched_review += 1
                    kind = self._store(source_id, spec, product, listing, match, query.text)
                    if kind == "new":
                        out.new_observations += 1
                    elif kind == "changed":
                        out.price_changes += 1
                    else:
                        out.refreshed += 1
                if strong and self.stop_on_strong_match:
                    break  # staged search: specific query already found the product
        out.calls_made = source.client.calls_made

        stored = out.new_observations + out.price_changes + out.refreshed
        if out.errors and not stored:
            out.status = RunStatus.FAILED if out.queries == 0 else RunStatus.PARTIAL
        elif out.errors:
            out.status = RunStatus.PARTIAL
        else:
            out.status = RunStatus.SUCCESS
        self.pipeline.runs.finish(
            run_id, out.status, records_collected=stored,
            records_failed=len(out.errors), error_summary="; ".join(out.errors[:10]) or None,
        )
        if out.status in (RunStatus.SUCCESS, RunStatus.PARTIAL):
            self.pipeline.sources.mark_success(source_id)
        if out.errors:
            self.pipeline.sources.mark_error(source_id, "; ".join(out.errors[:3]))
        return out

    def search_all(
        self, product_ids: Optional[list[int]] = None, reanalyze: bool = True
    ) -> list[SearchOutcome]:
        products = [
            p for p in self.pipeline.products.list_all()
            if product_ids is None or p.id in product_ids
        ]
        outcomes: list[SearchOutcome] = []
        for spec in self.specs:
            try:
                outcomes.append(self.search_source(spec, products))
            except Exception as exc:  # noqa: BLE001 - never crash the app
                logger.exception("supplier search failed: %s", spec.source_name)
                outcomes.append(SearchOutcome(spec.source_name, RunStatus.FAILED, errors=[str(exc)]))
        if reanalyze and any(o.new_observations or o.price_changes or o.refreshed for o in outcomes):
            self.pipeline.reanalyze()
        return outcomes


def build_source_specs(settings) -> list[SourceSpec]:
    """Source specs from settings; sources lacking credentials get factory=None."""
    from ..collectors.http_client import JsonHttpClient

    cap = settings.supplier_max_calls_per_run
    ebay_ok = bool(settings.ebay_client_id and settings.ebay_client_secret)
    rakuten_ok = bool(settings.rakuten_application_id and settings.rakuten_access_key)
    return [
        SourceSpec.for_class(
            EbayBrowseSource, "US",
            (lambda: EbayBrowseSource(
                settings.ebay_client_id, settings.ebay_client_secret,
                client=JsonHttpClient(max_calls=cap, min_interval_s=0.3),
                marketplace_id=settings.ebay_marketplace_id,
                delivery_country=settings.ebay_delivery_country,
            )) if ebay_ok else None,
            "EBAY_CLIENT_ID / EBAY_CLIENT_SECRET 환경변수가 설정되지 않아 건너뜀",
        ),
        SourceSpec.for_class(
            RakutenIchibaSource, "JP",
            (lambda: RakutenIchibaSource(
                settings.rakuten_application_id, settings.rakuten_access_key,
                client=JsonHttpClient(max_calls=cap, min_interval_s=1.0),
            )) if rakuten_ok else None,
            "RAKUTEN_APPLICATION_ID / RAKUTEN_ACCESS_KEY 환경변수가 설정되지 않아 건너뜀",
        ),
    ]
