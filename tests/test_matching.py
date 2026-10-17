"""Normalization, keyword expansion and demand<->product matching tests (Phase 2)."""
from __future__ import annotations

from pathlib import Path

from config.settings import BRAND_ALIASES_PATH, load_scoring_config
from src.analysis.scoring import Scorer
from src.discovery.keyword_expander import generate_keyword_candidates
from src.discovery.product_normalizer import (
    KeywordDictionary,
    normalize_model,
    normalize_text,
    tokenize,
)
from src.matching.product_matcher import match_keyword_to_products
from src.models.enums import KeywordLinkStatus
from src.models.product import Product, ProductKeyword
from src.services.pipeline import AnalysisPipeline
from src.storage.repositories import ProductKeywordRepository, ProductRepository

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "data" / "sample"
D = KeywordDictionary.from_mapping({
    "brands": {"Dyson": ["다이슨"], "Stanley": ["스탠리"]},
    "categories": {"beauty": ["고데기"], "kitchen": ["텀블러"]},
    "product_aliases": {"HS05": ["에어랩"]},
})


def _products():
    ps = [
        Product(id=1, brand="Dyson", product_name="Dyson Airwrap Complete", model_number="HS05",
                category="beauty"),
        Product(id=2, brand="Stanley", product_name="Stanley Quencher H2.0", model_number="10-10827",
                gtin="0123456789012", category="kitchen"),
    ]
    return ps


def test_normalize():
    assert normalize_text("  Dyson  AIRWRAP!! ") == "dyson airwrap"
    assert tokenize("Quencher H2.0 - 40oz") == ["quencher", "h2.0", "40oz"]
    assert normalize_model("10-10827") == normalize_model("1010827") == "1010827"


def test_identifier_matches_are_auto():
    r = match_keyword_to_products("hs05 정품", _products(), D)
    assert [(m.product_id, m.status, m.method) for m in r] == [(1, KeywordLinkStatus.AUTO_LINKED, "model_number")]
    r = match_keyword_to_products("10 10827", _products(), D)  # split model number
    assert r[0].product_id == 2 and r[0].method == "model_number"
    r = match_keyword_to_products("0123456789012", _products(), D)
    assert r[0].method == "gtin"


def test_brand_alias_plus_nickname_is_auto():
    r = match_keyword_to_products("다이슨 에어랩", _products(), D)
    assert r[0].status == KeywordLinkStatus.AUTO_LINKED and r[0].method == "brand_and_name"


def test_uncertain_matches_need_review():
    assert match_keyword_to_products("스탠리 텀블러", _products(), D)[0].status == KeywordLinkStatus.NEEDS_REVIEW
    assert match_keyword_to_products("다이슨", _products(), D)[0].method == "brand_only"
    assert match_keyword_to_products("에어랩", _products(), D)[0].status == KeywordLinkStatus.NEEDS_REVIEW
    assert match_keyword_to_products("아이폰 케이스", _products(), D) == []


def test_ambiguous_auto_links_downgraded():
    ps = _products() + [Product(id=3, brand="Dyson", product_name="Dyson Airwrap Origin",
                                model_number="HS07", category="beauty")]
    r = match_keyword_to_products("dyson airwrap", ps, D)
    assert {m.product_id for m in r} == {1, 3}
    assert all(m.status == KeywordLinkStatus.NEEDS_REVIEW for m in r)


def test_keyword_candidates_ordered_and_deduped():
    cands = generate_keyword_candidates(_products()[0], D)
    kws = [c.keyword for c in cands]
    assert kws[0] == "HS05" and kws[1] == "Dyson HS05"
    assert "다이슨 에어랩" in kws and "다이슨 고데기" in kws
    assert len({c.normalized for c in cands}) == len(cands)


def test_user_decision_never_overwritten(db):
    conn = db.connect()
    pid = ProductRepository(conn).add(Product(product_name="X", brand="B"))
    repo = ProductKeywordRepository(conn)
    kid, _ = repo.upsert(ProductKeyword(product_id=pid, keyword="b x", normalized_keyword="b x",
                                        status=KeywordLinkStatus.NEEDS_REVIEW, match_confidence=0.5))
    repo.set_status(kid, KeywordLinkStatus.REJECTED)
    _, changed = repo.upsert(ProductKeyword(product_id=pid, keyword="b x", normalized_keyword="b x",
                                            status=KeywordLinkStatus.AUTO_LINKED, match_confidence=0.9))
    assert not changed and repo.get(kid).status == KeywordLinkStatus.REJECTED


def test_rerun_is_idempotent_and_keeps_history(db, converter):
    p = AnalysisPipeline(db, Scorer(load_scoring_config()), converter,
                         dictionary=KeywordDictionary.load(BRAND_ALIASES_PATH))
    p.run_from_directory(SAMPLE_DIR)
    counts = (len(p.products.list_all()), len(p.demand.list_all()), len(p.keywords.list_all()))
    second = p.run_from_directory(SAMPLE_DIR)
    assert second.products_imported == 0 and second.demand_signals_imported == 0
    assert counts == (len(p.products.list_all()), len(p.demand.list_all()), len(p.keywords.list_all()))
    assert len(p.opportunities.list_latest()) == 5 and len(p.opportunities.list_all()) == 10


def test_only_linked_keywords_feed_demand_and_confirmation_applies(db, converter):
    p = AnalysisPipeline(db, Scorer(load_scoring_config()), converter,
                         dictionary=KeywordDictionary.load(BRAND_ALIASES_PATH))
    p.run_from_directory(SAMPLE_DIR)
    stanley = p.products.find_by_model_number("10-10827")[0]
    opp = p.opportunities.latest_for_product(stanley.id)
    assert opp.demand_score == 0 and "demand_link_needs_review" in opp.risk_flags

    pending = [k for k in p.keywords.list_by_product(stanley.id)
               if k.status == KeywordLinkStatus.NEEDS_REVIEW]
    p.keywords.set_status(pending[0].id, KeywordLinkStatus.CONFIRMED)
    p.reanalyze()
    opp2 = p.opportunities.latest_for_product(stanley.id)
    assert opp2.demand_score > 0
    assert opp2.score_basis["demand"]["linked_keywords"][0]["status"] == "confirmed"


def test_score_basis_has_provenance(db, converter):
    p = AnalysisPipeline(db, Scorer(load_scoring_config()), converter,
                         dictionary=KeywordDictionary.load(BRAND_ALIASES_PATH))
    p.run_from_directory(SAMPLE_DIR)
    for o in p.opportunities.list_latest():
        b = o.score_basis
        assert {"weights", "inputs", "cost", "fx", "supplier_offer", "domestic_offer"} <= set(b)
        assert b["fx"]["provider"] and b["supplier_offer"]["checked_at"]
