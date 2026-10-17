"""Expert-review fixes: realistic cost model, decision tiers, ranking, regulation flags."""
from __future__ import annotations

from pathlib import Path

import pytest

from config.settings import BRAND_ALIASES_PATH, load_scoring_config
from src.analysis.cost_model import CostAssumptions, ImportRule, match_import_rules
from src.analysis.scoring import Scorer
from src.discovery.product_normalizer import KeywordDictionary
from src.models.enums import Confidence, VerificationStatus
from src.models.product import Product
from src.services.pipeline import TIER_CONDITIONAL, TIER_EXCLUDE, TIER_SOURCE, AnalysisPipeline, decision_tier

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "data" / "sample"


@pytest.fixture
def ca():
    return CostAssumptions.from_config(load_scoring_config())


@pytest.fixture
def pipe(db, converter):
    p = AnalysisPipeline(db, Scorer(load_scoring_config()), converter,
                         dictionary=KeywordDictionary.load(BRAND_ALIASES_PATH))
    p.run_from_directory(SAMPLE_DIR)
    return p


def test_customs_duty_free_thresholds(ca, converter):
    p = Product(product_name="x", category="toys")
    usd = converter.get_rate("USD").rate_to_krw
    # US goods: free up to US$200; others: US$150.
    assert ca.customs(p, "US", 180 * usd, 10000, converter).value_krw == 0
    assert ca.customs(p, "DE", 180 * usd, 10000, converter).value_krw > 0
    est = ca.customs(p, "DE", 300 * usd, 20000, converter)
    value = 300 * usd + 20000
    assert est.value_krw == pytest.approx(value * 0.08 + value * 1.08 * 0.10)
    assert "추정" in est.basis


def test_forwarding_estimate_uses_weight_rate_and_minimum(ca):
    heavy = Product(product_name="x", weight_kg=2.5)
    light = Product(product_name="x", weight_kg=0.3)
    assert ca.international_shipping(heavy, "JP").value_krw == pytest.approx(2.5 * 8000)
    assert ca.international_shipping(light, "US").value_krw == pytest.approx(1.0 * 12000)  # min weight
    assert ca.international_shipping(Product(product_name="x"), "DE").value_krw == pytest.approx(2.0 * 15000)


def test_channels_from_config(ca):
    assert ca.channel("coupang").fee_pct > ca.channel("smartstore").fee_pct
    assert ca.channel("does-not-exist").key == ca.default_channel


def test_decision_tier():
    assert decision_tier(-1, 20, []) == TIER_EXCLUDE
    assert decision_tier(None, 20, []) == TIER_EXCLUDE
    assert decision_tier(10, 20, []) == TIER_CONDITIONAL
    assert decision_tier(25, 20, ["price_uncompetitive"]) == TIER_CONDITIONAL
    assert decision_tier(25, 20, []) == TIER_SOURCE


def test_import_rules_match():
    rules = [ImportRule("battery", "b", ("power bank", "보조배터리"), ()),
             ImportRule("kc", "k", (), ("electronics",))]
    hit = match_import_rules(Product(product_name="Anker Power Bank 10000", category="electronics"), rules)
    assert {r.tag for r in hit} == {"battery", "kc"}
    assert match_import_rules(Product(product_name="LEGO Orchid", category="toys"), rules) == []


def test_loss_maker_never_ranked_first(pipe):
    ranked = pipe.ranked_latest()
    tiers = [o.score_basis["decision"]["tier"] for o in ranked]
    assert tiers == sorted(tiers, key=[TIER_SOURCE, TIER_CONDITIONAL, TIER_EXCLUDE].index)
    dyson = pipe.products.find_by_model_number("HS05")[0]
    d = pipe.opportunities.latest_for_product(dyson.id)
    assert "negative_margin" in d.risk_flags and d.score_basis["decision"]["tier"] == TIER_EXCLUDE
    assert ranked[0].product_id != dyson.id
    # SQL-level ordering also puts loss-makers after profitable ones.
    margins = [o.estimated_margin_pct for o in pipe.opportunities.list_latest()]
    first_loss = next(i for i, m in enumerate(margins) if m is not None and m < 0)
    assert all(m >= 0 for m in margins[:first_loss])


def test_costs_include_shipping_customs_and_fees(pipe):
    dyson = pipe.products.find_by_model_number("HS05")[0]
    bd = pipe.opportunities.latest_for_product(dyson.id).score_basis["cost"]["breakdown"]
    for key in ("international_shipping_krw", "customs_tax_krw", "payment_fee_krw",
                "fx_buffer_krw", "return_allowance_krw", "platform_fee_krw"):
        assert bd[key] > 0, key
    # Card fee/FX buffer are charged on the overseas payment only.
    paid = bd["product_cost_krw"] + bd["foreign_shipping_krw"]
    assert bd["payment_fee_krw"] == pytest.approx(paid * 0.025, rel=1e-3)


def test_required_sale_price_and_uncompetitive_flag(pipe):
    for o in pipe.opportunities.list_latest():
        dec = o.score_basis["decision"]
        req, mkt = dec["required_sale_price_krw"], dec["market_lowest_krw"]
        assert req and mkt
        assert ("price_uncompetitive" in o.risk_flags) == (req > mkt)


def test_my_sale_price_used_and_flagged(pipe):
    stanley = pipe.products.find_by_model_number("10-10827")[0]
    pipe.products.update_fields(stanley.id, my_sale_price_krw=120000)
    pipe.reanalyze()
    o = pipe.opportunities.latest_for_product(stanley.id)
    assert o.score_basis["decision"]["sale_price_krw"] == 120000
    assert o.score_basis["decision"]["sale_price_source"] == "my_sale_price"
    assert "my_price_above_market" in o.risk_flags


def test_channel_changes_margin(db, converter):
    cfg = load_scoring_config()
    d = KeywordDictionary.load(BRAND_ALIASES_PATH)
    a = AnalysisPipeline(db, Scorer(cfg), converter, dictionary=d, channel="smartstore")
    a.run_from_directory(SAMPLE_DIR)
    m_smart = {o.product_id: o.estimated_margin_pct for o in a.opportunities.list_latest()}
    assert a.set_channel("coupang")
    a.reanalyze()
    m_coupang = {o.product_id: o.estimated_margin_pct for o in a.opportunities.list_latest()}
    assert all(m_coupang[k] < m_smart[k] for k in m_smart)


def test_regulation_and_lead_time_flags(pipe):
    anker = pipe.products.find_by_model_number("A1263")[0]
    flags = pipe.opportunities.latest_for_product(anker.id).risk_flags
    assert "reg:battery" in flags and "reg:kc_electric" in flags
    lego = pipe.products.find_by_model_number("10311")[0]
    assert "long_lead_time" in pipe.opportunities.latest_for_product(lego.id).risk_flags  # 18 days > 14


def test_confidence_capped_by_unverified_offer(pipe):
    dyson = pipe.products.find_by_model_number("HS05")[0]
    o = pipe.opportunities.latest_for_product(dyson.id)
    assert o.score_basis["supplier_offer"]["verification_status"] == VerificationStatus.UNVERIFIED.value
    assert o.confidence in (Confidence.UNVERIFIED, Confidence.INSUFFICIENT_DATA)


def test_weight_from_csv_updates_existing_product(db, converter, tmp_path):
    import shutil

    for f in ("products.csv", "supplier_offers.csv", "domestic_offers.csv"):
        shutil.copy(SAMPLE_DIR / f, tmp_path / f)
    p = AnalysisPipeline(db, Scorer(load_scoring_config()), converter)
    p.run_from_directory(tmp_path)
    text = (tmp_path / "products.csv").read_text(encoding="utf-8").replace("electronics,black,CN,0.3", "electronics,black,CN,2.0")
    (tmp_path / "products.csv").write_text(text, encoding="utf-8")
    p.run_from_directory(tmp_path)
    assert p.products.find_by_model_number("A1263")[0].weight_kg == 2.0
