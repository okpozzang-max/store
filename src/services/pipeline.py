"""Analysis pipeline service (PRD 5, 9, 12-integration).

End-to-end flow:
    CSV import (products, offers, demand signals)
      -> keyword candidate generation (rule-based, user dictionary)
      -> demand keyword <-> product linking (auto vs. needs-review)
      -> unit economics -> scoring (with score basis) -> opportunity history
      -> CSV export.

Re-running on the same input is idempotent for stored data (products,
offers, signals, keyword links are de-duplicated). Each analysis appends a
new opportunity row per product so scoring history is preserved; the latest
row is the current view and carries over the user's review status.

A failure in one file/source never aborts the others; errors are summarized
in a collection_runs record.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..analysis.cost_model import CostAssumptions, ImportRule, load_import_rules, match_import_rules
from ..analysis.demand_analysis import aggregate_demand_indices
from ..analysis.scoring import ScoreInputs, Scorer
from ..analysis.supplier_trust import assess_offer_trust
from ..analysis.unit_economics import calculate_costs
from ..collectors.csv_source import (
    DemandSignalCsvCollector,
    DomesticOfferCsvCollector,
    ProductCsvCollector,
    SupplierOfferCsvCollector,
)
from ..discovery.keyword_expander import generate_keyword_candidates
from ..discovery.product_normalizer import KeywordDictionary, normalize_text
from ..matching.product_matcher import match_keyword_to_products
from ..models.demand import DataSource, DemandSignal
from ..models.economics import CostInputs, Opportunity
from ..models.enums import (
    LINKED_STATUSES,
    PRICING_EXCLUDED_STATUSES,
    Confidence,
    KeywordLinkStatus,
    ReviewStatus,
    RunStatus,
    VerificationStatus,
)
from ..models.product import Product, ProductKeyword
from ..models.supplier import DomesticOffer, SupplierOffer
from ..storage.database import Database
from ..storage.repositories import (
    CollectionRunRepository,
    DataSourceRepository,
    DemandSignalRepository,
    DomesticOfferRepository,
    OpportunityRepository,
    ProductKeywordRepository,
    ProductRepository,
    SupplierOfferRepository,
    SupplierRepository,
)
from ..utils.currency import CurrencyConverter, UnsupportedCurrencyError
from ..utils.logging_config import get_logger

logger = get_logger(__name__)

IMPORT_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "import_rules.yaml"

# Decision tiers shown in the UI (PRD 8.4: never auto-promote doubtful candidates).
TIER_SOURCE = "source"            # 소싱 검토: target margin met at market price
TIER_CONDITIONAL = "conditional"  # 조건부: profitable but below target / uncompetitive
TIER_EXCLUDE = "exclude"          # 제외 권고: loss-making or not evaluable
_CONFIDENCE_RANK = {
    Confidence.VERIFIED: 3, Confidence.PARTIALLY_VERIFIED: 2,
    Confidence.UNVERIFIED: 1, Confidence.INSUFFICIENT_DATA: 0,
}


def decision_tier(margin_pct: Optional[float], target: float, flags: list[str]) -> str:
    if margin_pct is None or margin_pct < 0:
        return TIER_EXCLUDE
    if margin_pct < target or "price_uncompetitive" in flags:
        return TIER_CONDITIONAL
    return TIER_SOURCE

DEFAULT_CSV_SOURCE = "CSV 가져오기"
SCORING_NOTE = (
    "가중치는 초기 가설입니다. 수요 지수는 검색/클릭 상대 지수이며 실제 판매량이 아닙니다. "
    "경쟁 점수는 국내 비교 판매처 수 기반 추정치입니다."
)


@dataclass
class ImportSummary:
    products_imported: int = 0
    supplier_offers_imported: int = 0
    domestic_offers_imported: int = 0
    demand_signals_imported: int = 0
    keyword_candidates_created: int = 0
    keyword_links_auto: int = 0
    keyword_links_review: int = 0
    opportunities_created: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class LinkSummary:
    auto_linked: int = 0
    needs_review: int = 0
    unmatched_keywords: list[str] = field(default_factory=list)


class AnalysisPipeline:
    """Runs import, demand linking and scoring against a Database."""

    def __init__(
        self,
        db: Database,
        scorer: Scorer,
        converter: CurrencyConverter,
        target_margin_pct: float = 20.0,
        dictionary: Optional[KeywordDictionary] = None,
        channel: Optional[str] = None,
        import_rules: Optional[list[ImportRule]] = None,
    ) -> None:
        conn = db.connect()
        self.db = db
        self.scorer = scorer
        self.converter = converter
        self.target_margin_pct = target_margin_pct
        self.dictionary = dictionary or KeywordDictionary()
        config = scorer.config if hasattr(scorer, "config") else {}
        business = config.get("business", {}) or {}
        self.costs = CostAssumptions.from_config(config)
        self.channel = self.costs.channel(channel)
        self.import_rules = import_rules if import_rules is not None else load_import_rules(IMPORT_RULES_PATH)
        self.min_supplier_trust = float(business.get("min_supplier_trust", 40.0))

        self.products = ProductRepository(conn)
        self.supplier_offers = SupplierOfferRepository(conn)
        self.suppliers = SupplierRepository(conn)
        self.domestic_offers = DomesticOfferRepository(conn)
        self.opportunities = OpportunityRepository(conn)
        self.runs = CollectionRunRepository(conn)
        self.sources = DataSourceRepository(conn)
        self.demand = DemandSignalRepository(conn)
        self.keywords = ProductKeywordRepository(conn)

    # ------------------------------------------------------------------ sources

    def ensure_csv_source(self, name: str = DEFAULT_CSV_SOURCE, notes: Optional[str] = None) -> int:
        return self.sources.ensure(
            DataSource(
                name=name,
                source_type="csv",
                country="KR",
                access_method="csv",
                license_notes=notes or "사용자가 직접 준비한 CSV 파일",
            )
        )

    # ------------------------------------------------------------------ CSV import

    def import_products_csv(self, path: str | Path) -> tuple[int, list[str]]:
        result = ProductCsvCollector().collect(path)
        created = 0
        for product in result.records:
            _, is_new = self.products.upsert(product)
            created += int(is_new)
        return created, result.errors

    def import_supplier_offers_csv(
        self, path: str | Path, source_id: Optional[int] = None
    ) -> tuple[int, list[str]]:
        result = SupplierOfferCsvCollector().collect(path)
        errors = list(result.errors)
        created = 0
        for product_ref, offer in result.records:
            pid = self._resolve_product(product_ref)
            if pid is None:
                errors.append(f"supplier offer: unknown product_ref '{product_ref}'")
                continue
            offer.product_id = pid
            offer.source_id = offer.source_id or source_id
            self.suppliers.ensure(offer.supplier_name, offer.supplier_country, offer.supplier_type)
            _, is_new = self.supplier_offers.add_if_new(offer)
            created += int(is_new)
        return created, errors

    def import_domestic_offers_csv(
        self, path: str | Path, source_id: Optional[int] = None
    ) -> tuple[int, list[str]]:
        result = DomesticOfferCsvCollector().collect(path)
        errors = list(result.errors)
        created = 0
        for product_ref, offer in result.records:
            pid = self._resolve_product(product_ref)
            if pid is None:
                errors.append(f"domestic offer: unknown product_ref '{product_ref}'")
                continue
            offer.product_id = pid
            offer.source_id = offer.source_id or source_id
            _, is_new = self.domestic_offers.add_if_new(offer)
            created += int(is_new)
        return created, errors

    def import_demand_signals_csv(
        self, path: str | Path, source_id: Optional[int] = None
    ) -> tuple[int, list[str]]:
        result = DemandSignalCsvCollector().collect(path)
        created = 0
        for signal in result.records:
            signal.source_id = signal.source_id or source_id
            created += int(self.store_signal(signal))
        return created, result.errors

    def store_signal(self, signal: DemandSignal) -> bool:
        """Persist a demand signal unless it is a duplicate. Returns True if new."""
        _, is_new = self.demand.add_if_new(signal)
        return is_new

    def _resolve_product(self, product_ref: str) -> Optional[int]:
        """Resolve a product_ref by model number, then GTIN, then product name."""
        ref = (product_ref or "").strip()
        if not ref:
            return None
        by_model = self.products.find_by_model_number(ref)
        if by_model:
            return by_model[0].id
        if ref.isdigit():
            by_gtin = self.products.find_by_gtin(ref)
            if by_gtin:
                return by_gtin[0].id
        for product in self.products.list_all():
            if product.product_name == ref:
                return product.id
        return None

    # ------------------------------------------------------------------ keywords

    def generate_candidates(self) -> int:
        """Store rule-based keyword candidates for every product."""
        created = 0
        for product in self.products.list_all():
            assert product.id is not None
            for cand in generate_keyword_candidates(product, self.dictionary):
                _, changed = self.keywords.upsert(
                    ProductKeyword(
                        product_id=product.id,
                        keyword=cand.keyword,
                        normalized_keyword=cand.normalized,
                        origin="generated",
                        status=KeywordLinkStatus.CANDIDATE,
                        reason=cand.reason,
                    )
                )
                created += int(changed)
        return created

    def query_keywords(self) -> list[tuple[int, str]]:
        """(product_id, keyword) pairs worth collecting demand for.

        Rejected links are excluded; everything else (candidates, links,
        pending reviews) is a valid query term.
        """
        out = []
        for pk in self.keywords.list_all():
            if pk.status != KeywordLinkStatus.REJECTED:
                out.append((pk.product_id, pk.keyword))
        return out

    def link_demand_signals(self) -> LinkSummary:
        """Match every observed demand keyword against products (PRD 9.3)."""
        summary = LinkSummary()
        products = self.products.list_all()
        seen: set[str] = set()
        for signal in self.demand.list_all():
            norm = normalize_text(signal.keyword)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            matches = match_keyword_to_products(signal.keyword, products, self.dictionary)
            if not matches:
                summary.unmatched_keywords.append(signal.keyword)
                continue
            for m in matches:
                self.keywords.upsert(
                    ProductKeyword(
                        product_id=m.product_id,
                        keyword=signal.keyword,
                        normalized_keyword=norm,
                        origin="demand_match",
                        match_method=m.method,
                        match_confidence=m.confidence,
                        status=m.status,
                        reason=m.reason,
                    )
                )
                current = self.keywords.get_by_key(m.product_id, norm)
                if current and current.status in LINKED_STATUSES:
                    summary.auto_linked += 1
                elif current and current.status == KeywordLinkStatus.NEEDS_REVIEW:
                    summary.needs_review += 1
        return summary

    # ------------------------------------------------------------------ demand

    def _signals_by_keyword(self) -> dict[str, list[DemandSignal]]:
        index: dict[str, list[DemandSignal]] = {}
        for s in self.demand.list_all():
            index.setdefault(normalize_text(s.keyword), []).append(s)
        return index

    @staticmethod
    def _latest_per_source(signals: list[DemandSignal]) -> list[DemandSignal]:
        """Keep the most recent observation per (source, signal type)."""
        latest: dict[tuple, DemandSignal] = {}
        for s in signals:
            key = (s.source_id, s.signal_type.value)
            ts = s.period_end or s.observed_at or datetime.min
            cur = latest.get(key)
            cur_ts = (cur.period_end or cur.observed_at or datetime.min) if cur else None
            if cur is None or ts >= cur_ts:
                latest[key] = s
        return list(latest.values())

    def demand_for_product(
        self, product_id: int, index: Optional[dict[str, list[DemandSignal]]] = None
    ) -> dict[str, Any]:
        """Aggregate demand evidence from the product's *linked* keywords only."""
        index = index if index is not None else self._signals_by_keyword()
        links = self.keywords.list_by_product(product_id)
        linked = [k for k in links if k.status in LINKED_STATUSES]
        pending = [k for k in links if k.status == KeywordLinkStatus.NEEDS_REVIEW]

        used: list[DemandSignal] = []
        for k in linked:
            used.extend(self._latest_per_source(index.get(k.normalized_keyword, [])))

        demand_index = aggregate_demand_indices([s.demand_index for s in used]) if used else None
        growths = [s.growth_pct for s in used if s.growth_pct is not None]
        growth_pct = round(sum(growths) / len(growths), 2) if growths else None
        source_ids = {s.source_id for s in used}

        return {
            "demand_index": demand_index,
            "growth_pct": growth_pct,
            "source_count": len(source_ids),
            "linked_keywords": [
                {"keyword": k.keyword, "status": k.status.value, "method": k.match_method,
                 "confidence": k.match_confidence, "reason": k.reason}
                for k in linked
            ],
            "review_pending_keywords": [
                {"keyword": k.keyword, "method": k.match_method, "reason": k.reason}
                for k in pending
            ],
            "signals": [
                {
                    "keyword": s.keyword,
                    "source": self._source_name(s.source_id),
                    "signal_type": s.signal_type.value,
                    "period_start": s.period_start.isoformat() if s.period_start else None,
                    "period_end": s.period_end.isoformat() if s.period_end else None,
                    "demand_index": s.demand_index,
                    "growth_pct": s.growth_pct,
                    "confidence": s.confidence.value,
                    "raw_reference": s.raw_reference,
                }
                for s in used
            ],
        }

    def _source_name(self, source_id: Optional[int]) -> Optional[str]:
        if source_id is None:
            return None
        src = self.sources.get(source_id)
        return src.name if src else str(source_id)

    # ------------------------------------------------------------------ offers

    def _latest_observations(self, offers: list, key_fn) -> list:
        latest: dict[tuple, Any] = {}
        for o in offers:
            k = key_fn(o)
            ts = o.price_checked_at or datetime.min
            cur = latest.get(k)
            if cur is None or ts >= (cur.price_checked_at or datetime.min):
                latest[k] = o
        return list(latest.values())

    def _shipping_in_offer_currency(self, o: SupplierOffer) -> float:
        """Shipping expressed in the offer's own currency (handles a different shipping currency)."""
        if not o.shipping_cost:
            return 0.0
        ship_cur = (o.shipping_currency or o.currency).upper()
        if ship_cur == o.currency.upper():
            return o.shipping_cost
        krw = self.converter.to_krw(o.shipping_cost, ship_cur)
        return krw / self.converter.get_rate(o.currency).rate_to_krw

    def supplier_candidates(self, product_id: int) -> list[dict[str, Any]]:
        """Latest observation per supplier listing, with trust and eligibility.

        Eligible = identity not in review/rejected, trust not excluded and a
        known exchange rate. Sorted: eligible first, then by landed KRW price.
        The cheapest offer is only chosen among eligible ones (PRD 9.4).
        """
        offers = self._latest_observations(
            self.supplier_offers.list_by_product(product_id),
            lambda o: (o.supplier_name, o.product_url),
        )
        out: list[dict[str, Any]] = []
        for o in offers:
            supplier = self.suppliers.get_by_name(o.supplier_name)
            trust = assess_offer_trust(o, supplier, self.min_supplier_trust)
            reason: Optional[str] = None
            if o.verification_status == VerificationStatus.REJECTED:
                reason = "사용자 제외"
            elif o.verification_status == VerificationStatus.NEEDS_REVIEW:
                reason = "동일 상품 여부 검토 필요"
            elif trust.excluded:
                reason = trust.exclusion_reason
            landed: Optional[float] = None
            try:
                landed = self.converter.to_krw(
                    o.foreign_price + self._shipping_in_offer_currency(o), o.currency
                )
            except UnsupportedCurrencyError:
                reason = reason or f"환율 미등록 통화({o.currency}) — 임의 환율로 계산하지 않음"
            out.append({
                "offer": o, "supplier": supplier, "trust": trust,
                "landed_krw": landed, "eligible": reason is None, "excluded_reason": reason,
            })
        out.sort(key=lambda c: (not c["eligible"], c["landed_krw"] if c["landed_krw"] is not None else float("inf")))
        return out

    def domestic_candidates(self, product_id: int) -> list[DomesticOffer]:
        """Latest observation per domestic listing usable for comparison."""
        offers = self._latest_observations(
            self.domestic_offers.list_by_product(product_id),
            lambda o: (o.seller_name, o.product_url),
        )
        return [o for o in offers if o.verification_status not in PRICING_EXCLUDED_STATUSES]

    def _best_supplier_offer(self, product_id: int) -> Optional[SupplierOffer]:
        cands = self.supplier_candidates(product_id)
        return cands[0]["offer"] if cands and cands[0]["eligible"] else None

    def _best_domestic_offer(self, product_id: int) -> Optional[DomesticOffer]:
        offers = self.domestic_candidates(product_id)
        # Conservative: compare against the lowest delivered domestic price.
        return min(offers, key=lambda o: o.total_price_krw) if offers else None

    # ------------------------------------------------------------------ analysis

    def analyze(self) -> int:
        """Score every product that has a supplier offer and a domestic offer."""
        created = 0
        index = self._signals_by_keyword()
        for product in self.products.list_all():
            try:
                if self._analyze_product(product, index):
                    created += 1
            except UnsupportedCurrencyError as exc:
                logger.warning("Skipping product %s: %s", product.id, exc)
        return created

    def _record_not_evaluable(
        self,
        product: Product,
        candidates: list[dict[str, Any]],
        domestic: Optional[DomesticOffer],
        previous: Optional[Opportunity],
    ) -> None:
        assert product.id is not None
        flags = []
        if not any(c["eligible"] for c in candidates):
            flags.append("no_eligible_supplier_offer")
        if domestic is None:
            flags.append("no_domestic_offer")
        self.opportunities.add(
            Opportunity(
                product_id=product.id,
                demand_score=0, growth_score=0, price_score=0, margin_score=0,
                competition_score=0, total_score=0,
                risk_flags=flags,
                confidence=Confidence.INSUFFICIENT_DATA,
                review_status=previous.review_status if previous else ReviewStatus.NEW,
                score_basis={
                    "weights": dict(self.scorer.weights),
                    "decision": {"tier": TIER_EXCLUDE},
                    "inputs": {},
                    "not_evaluable": True,
                    "supplier_candidates": [
                        {
                            "supplier_name": c["offer"].supplier_name,
                            "price": c["offer"].foreign_price,
                            "currency": c["offer"].currency,
                            "landed_krw": round(c["landed_krw"], 0) if c["landed_krw"] is not None else None,
                            "verification_status": c["offer"].verification_status.value,
                            "trust_score": c["trust"].score,
                            "eligible": c["eligible"],
                            "excluded_reason": c["excluded_reason"],
                            "url": c["offer"].product_url,
                        }
                        for c in candidates[:10]
                    ],
                    "note": "가격 비교에 쓸 수 있는 오퍼가 없어 점수를 산출하지 않았습니다. "
                            "상품 상세에서 후보를 확정하거나 공급처 탐색을 실행하세요.",
                },
            )
        )

    def _analyze_product(self, product: Product, index: dict[str, list[DemandSignal]]) -> bool:
        assert product.id is not None
        candidates = self.supplier_candidates(product.id)
        best = candidates[0] if candidates and candidates[0]["eligible"] else None
        domestic_offers = self.domestic_candidates(product.id)
        domestic = min(domestic_offers, key=lambda o: o.total_price_krw) if domestic_offers else None
        if best is None or domestic is None:
            if candidates and best is None:
                logger.info("product %s: no eligible supplier offer (%d candidates)", product.id, len(candidates))
            previous = self.opportunities.latest_for_product(product.id)
            if (candidates and best is None) or previous is not None:
                # Record that the product can no longer be evaluated instead of
                # leaving an outdated score as the "current" one.
                self._record_not_evaluable(product, candidates, domestic, previous)
                return True
            return False
        supplier: SupplierOffer = best["offer"]
        trust = best["trust"]

        fx = self.converter.get_rate(supplier.currency)  # raises if unsupported
        ca = self.costs
        overseas_payment_krw = self.converter.to_krw(
            supplier.foreign_price + self._shipping_in_offer_currency(supplier), supplier.currency
        )
        intl = ca.international_shipping(product, supplier.supplier_country)
        customs = ca.customs(product, supplier.supplier_country, overseas_payment_krw,
                             intl.value_krw, self.converter)
        market_price = domestic.total_price_krw
        sale_price = product.my_sale_price_krw or market_price
        cost = calculate_costs(
            CostInputs(
                foreign_price=supplier.foreign_price,
                currency=supplier.currency,
                exchange_rate=fx.rate_to_krw,
                foreign_shipping_cost=self._shipping_in_offer_currency(supplier),
                international_shipping_krw=intl.value_krw,
                customs_tax_krw=customs.value_krw,
                other_costs_krw=ca.packaging_krw,
                domestic_sale_price_krw=sale_price,
                platform_fee_pct=self.channel.fee_pct,
                payment_fee_pct=ca.overseas_card_fee_pct,
                fx_buffer_pct=ca.fx_buffer_pct,
                return_allowance_pct=ca.return_allowance_pct,
            ),
            self.converter,
            target_margin_pct=self.target_margin_pct,
        )
        # Price opportunity vs. the market, using the FULL pre-sale-fee cost
        # (not just item + shipping) so missing costs cannot inflate it.
        price_gap_ratio = (market_price - cost.cost_before_sale_fees_krw) / market_price if market_price else None
        competitor_count = len(domestic_offers)

        demand = self.demand_for_product(product.id, index)
        score = self.scorer.score(
            ScoreInputs(
                demand_index=demand["demand_index"],
                growth_pct=demand["growth_pct"],
                price_gap_ratio=price_gap_ratio,
                margin_pct=cost.estimated_margin_pct,
                competitor_count=competitor_count,
            )
        )

        flags = list(score.risk_flags)
        if cost.estimated_margin_pct < 0:
            flags.append("negative_margin")
        if cost.required_sale_price_krw is None or cost.required_sale_price_krw > market_price:
            flags.append("price_uncompetitive")
        if product.my_sale_price_krw and product.my_sale_price_krw > market_price:
            flags.append("my_price_above_market")
        flags.append("intl_shipping_estimated")
        if customs.value_krw > 0:
            flags.append("customs_estimated")
        regulations = match_import_rules(product, self.import_rules)
        flags.extend(f"reg:{r.tag}" for r in regulations)
        if supplier.lead_time_days is not None and supplier.lead_time_days > ca.max_lead_time_days:
            flags.append("long_lead_time")
        if demand["demand_index"] is None and demand["review_pending_keywords"]:
            flags.append("demand_link_needs_review")
        if demand["demand_index"] is not None and demand["source_count"] < 2:
            flags.append("single_demand_source")
        if supplier.verification_status != VerificationStatus.VERIFIED:
            flags.append("supplier_offer_unverified")
        if not supplier.shipping_cost_known:
            flags.append("supplier_shipping_unknown")
        ships = supplier.ships_to_kr
        if ships is None and best["supplier"] is not None:
            ships = best["supplier"].ships_to_kr
        if ships is False:
            flags.append("forwarding_required")
        elif ships is None:
            flags.append("ships_to_kr_unknown")
        if domestic.verification_status != VerificationStatus.VERIFIED:
            flags.append("domestic_offer_unverified")
        if not domestic.shipping_cost_known:
            flags.append("domestic_shipping_unknown")
        if (cost.estimated_margin_pct or 0) < self.target_margin_pct:
            flags.append("below_target_margin")

        # Confidence reflects verification, not just data presence: an
        # unverified offer can never yield more than "unverified".
        confidence = score.confidence
        if {"supplier_offer_unverified", "domestic_offer_unverified"} & set(flags):
            if _CONFIDENCE_RANK[confidence] > _CONFIDENCE_RANK[Confidence.UNVERIFIED]:
                confidence = Confidence.UNVERIFIED
        tier = decision_tier(cost.estimated_margin_pct, self.target_margin_pct, flags)

        previous = self.opportunities.latest_for_product(product.id)
        opp = Opportunity(
            product_id=product.id,
            supplier_offer_id=supplier.id,
            domestic_offer_id=domestic.id,
            demand_score=score.demand_score,
            growth_score=score.growth_score,
            price_score=score.price_score,
            margin_score=score.margin_score,
            competition_score=score.competition_score,
            total_score=score.total_score,
            estimated_profit_krw=cost.estimated_profit_krw,
            estimated_margin_pct=cost.estimated_margin_pct,
            risk_flags=flags,
            confidence=confidence,
            review_status=previous.review_status if previous else ReviewStatus.NEW,
            score_basis={
                "weights": dict(self.scorer.weights),
                "decision": {
                    "tier": tier,
                    "sale_price_krw": sale_price,
                    "sale_price_source": "my_sale_price" if product.my_sale_price_krw else "domestic_lowest",
                    "market_lowest_krw": market_price,
                    "required_sale_price_krw": cost.required_sale_price_krw,
                    "target_margin_pct": self.target_margin_pct,
                    "channel": {"key": self.channel.key, "label": self.channel.label,
                                "fee_pct": self.channel.fee_pct},
                },
                "regulations": [{"tag": r.tag, "label": r.label} for r in regulations],
                "inputs": {
                    "demand_index": demand["demand_index"],
                    "growth_pct": demand["growth_pct"],
                    "price_gap_ratio": round(price_gap_ratio, 4) if price_gap_ratio is not None else None,
                    "margin_pct": cost.estimated_margin_pct,
                    "competitor_count": competitor_count,
                },
                "demand": demand,
                "cost": {
                    "breakdown": cost.breakdown,
                    "cost_before_sale_fees_krw": cost.cost_before_sale_fees_krw,
                    "total_cost_krw": cost.total_cost_krw,
                    "assumptions": {
                        "판매 채널 수수료": f"{self.channel.label} {self.channel.fee_pct:.1%} (판매가 기준)",
                        "해외 카드 수수료": f"{ca.overseas_card_fee_pct:.1%} (해외 결제액 기준)",
                        "환율 여유분": f"{ca.fx_buffer_pct:.1%} (해외 결제액 기준)",
                        "반품·CS 충당": f"{ca.return_allowance_pct:.1%} (판매가 기준)",
                        "포장·기타": f"{ca.packaging_krw:,.0f}원",
                        "판매처 배송비": (
                            "판매처 표기 배송비 반영" if supplier.shipping_cost_known
                            else "판매처 배송비 미표기 — 0으로 계산, 확인 필요"
                        ),
                        "국제배송비": intl.basis,
                        "관부가세": customs.basis,
                    },
                },
                "fx": {
                    "currency": fx.currency,
                    "rate_to_krw": fx.rate_to_krw,
                    "provider": fx.provider,
                    "fetched_at": fx.fetched_at.isoformat() if fx.fetched_at else None,
                    "basis": fx.basis,
                },
                "supplier_offer": {
                    "supplier_name": supplier.supplier_name,
                    "supplier_type": supplier.supplier_type.value,
                    "country": supplier.supplier_country,
                    "url": supplier.product_url,
                    "listing_title": supplier.listing_title,
                    "price": supplier.foreign_price,
                    "currency": supplier.currency,
                    "shipping_cost": supplier.shipping_cost,
                    "shipping_currency": supplier.shipping_currency or supplier.currency,
                    "shipping_cost_known": supplier.shipping_cost_known,
                    "landed_krw": round(best["landed_krw"], 2),
                    "lead_time_days": supplier.lead_time_days,
                    "checked_at": supplier.price_checked_at.isoformat() if supplier.price_checked_at else None,
                    "verification_status": supplier.verification_status.value,
                    "match_method": supplier.match_method,
                    "match_reason": supplier.match_reason,
                    "trust": trust.as_dict(),
                    "source": self._source_name(supplier.source_id),
                },
                "supplier_candidates": [
                    {
                        "supplier_name": c["offer"].supplier_name,
                        "price": c["offer"].foreign_price,
                        "currency": c["offer"].currency,
                        "landed_krw": round(c["landed_krw"], 0) if c["landed_krw"] is not None else None,
                        "verification_status": c["offer"].verification_status.value,
                        "trust_score": c["trust"].score,
                        "eligible": c["eligible"],
                        "excluded_reason": c["excluded_reason"],
                        "url": c["offer"].product_url,
                    }
                    for c in candidates[:10]
                ],
                "domestic_offer": {
                    "seller_name": domestic.seller_name,
                    "url": domestic.product_url,
                    "listing_title": domestic.listing_title,
                    "price_krw": domestic.price_krw,
                    "shipping_cost_krw": domestic.shipping_cost_krw,
                    "shipping_cost_known": domestic.shipping_cost_known,
                    "verification_status": domestic.verification_status.value,
                    "match_reason": domestic.match_reason,
                    "checked_at": domestic.price_checked_at.isoformat() if domestic.price_checked_at else None,
                    "source": self._source_name(domestic.source_id),
                },
                "competition": {"domestic_seller_count": competitor_count, "estimated": True},
                "note": SCORING_NOTE,
            },
        )
        self.opportunities.add(opp)
        return True

    # ------------------------------------------------------------------ orchestration

    def run_from_directory(
        self,
        data_dir: str | Path,
        source_name: str = DEFAULT_CSV_SOURCE,
        source_notes: Optional[str] = None,
    ) -> ImportSummary:
        """Import the standard CSV files from a directory and run the analysis.

        Expected files (each optional): products.csv, supplier_offers.csv,
        domestic_offers.csv, demand_signals.csv. A missing or broken file does
        not stop the others.
        """
        data_dir = Path(data_dir)
        summary = ImportSummary()
        source_id = self.ensure_csv_source(source_name, source_notes)
        run_id = self.runs.start(source_id)

        steps = [
            ("products.csv", lambda p: self.import_products_csv(p), "products_imported"),
            ("supplier_offers.csv", lambda p: self.import_supplier_offers_csv(p, source_id),
             "supplier_offers_imported"),
            ("domestic_offers.csv", lambda p: self.import_domestic_offers_csv(p, source_id),
             "domestic_offers_imported"),
            ("demand_signals.csv", lambda p: self.import_demand_signals_csv(p, source_id),
             "demand_signals_imported"),
        ]
        for filename, fn, attr in steps:
            path = data_dir / filename
            if not path.is_file():
                continue
            try:
                n, errs = fn(path)
                setattr(summary, attr, n)
                summary.errors.extend(errs)
            except Exception as exc:  # noqa: BLE001 - keep other files going
                summary.errors.append(f"{filename}: {exc}")

        for label, fn in (
            ("keyword candidates", self._step_candidates),
            ("demand linking", self._step_link),
            ("analyze", self._step_analyze),
        ):
            try:
                fn(summary)
            except Exception as exc:  # noqa: BLE001
                logger.exception("pipeline step failed: %s", label)
                summary.errors.append(f"{label}: {exc}")

        status = RunStatus.SUCCESS if not summary.errors else RunStatus.PARTIAL
        self.runs.finish(
            run_id,
            status,
            records_collected=(
                summary.products_imported
                + summary.supplier_offers_imported
                + summary.domestic_offers_imported
                + summary.demand_signals_imported
            ),
            records_failed=len(summary.errors),
            error_summary="; ".join(summary.errors[:10]) if summary.errors else None,
        )
        if not summary.errors:
            self.sources.mark_success(source_id)
        else:
            self.sources.mark_error(source_id, "; ".join(summary.errors[:3]))
        return summary

    def _step_candidates(self, summary: ImportSummary) -> None:
        summary.keyword_candidates_created = self.generate_candidates()

    def _step_link(self, summary: ImportSummary) -> None:
        link = self.link_demand_signals()
        summary.keyword_links_auto = link.auto_linked
        summary.keyword_links_review = link.needs_review

    def _step_analyze(self, summary: ImportSummary) -> None:
        summary.opportunities_created = self.analyze()

    def reanalyze(self) -> int:
        """Re-link demand and re-score (e.g. after a manual link decision)."""
        self.link_demand_signals()
        return self.analyze()

    def ranked_latest(self) -> list[Opportunity]:
        """Current candidates ordered by decision tier, then total score."""
        order = {TIER_SOURCE: 0, TIER_CONDITIONAL: 1, TIER_EXCLUDE: 2}
        opps = self.opportunities.list_latest()
        return sorted(
            opps,
            key=lambda o: (
                order.get(o.score_basis.get("decision", {}).get("tier", TIER_EXCLUDE), 2),
                -o.total_score,
            ),
        )

    def set_channel(self, key: Optional[str]) -> bool:
        """Switch the sales channel; returns True if it changed."""
        new = self.costs.channel(key)
        changed = new.key != self.channel.key
        self.channel = new
        return changed

    # ------------------------------------------------------------------ export

    def export_opportunities_csv(self, path: str | Path) -> int:
        """Export the current (latest) opportunity per product to CSV."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        opps = self.ranked_latest()
        products = {p.id: p for p in self.products.list_all()}
        fields = [
            "id", "product_id", "product_name", "model_number", "decision_tier", "total_score",
            "demand_score", "growth_score", "price_score", "margin_score",
            "competition_score", "estimated_profit_krw", "estimated_margin_pct",
            "required_sale_price_krw", "market_lowest_krw", "sales_channel",
            "confidence", "review_status", "risk_flags", "linked_keywords",
            "calculated_at",
        ]
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            for o in opps:
                p = products.get(o.product_id)
                linked = o.score_basis.get("demand", {}).get("linked_keywords", [])
                dec = o.score_basis.get("decision", {})
                writer.writerow(
                    {
                        "id": o.id,
                        "product_id": o.product_id,
                        "product_name": p.product_name if p else "",
                        "model_number": (p.model_number or "") if p else "",
                        "decision_tier": dec.get("tier", ""),
                        "total_score": o.total_score,
                        "demand_score": o.demand_score,
                        "growth_score": o.growth_score,
                        "price_score": o.price_score,
                        "margin_score": o.margin_score,
                        "competition_score": o.competition_score,
                        "estimated_profit_krw": o.estimated_profit_krw,
                        "estimated_margin_pct": o.estimated_margin_pct,
                        "required_sale_price_krw": dec.get("required_sale_price_krw"),
                        "market_lowest_krw": dec.get("market_lowest_krw"),
                        "sales_channel": (dec.get("channel") or {}).get("label", ""),
                        "confidence": o.confidence.value,
                        "review_status": o.review_status.value,
                        "risk_flags": "|".join(o.risk_flags),
                        "linked_keywords": "|".join(k["keyword"] for k in linked),
                        "calculated_at": o.calculated_at.isoformat() if o.calculated_at else "",
                    }
                )
        return len(opps)
