"""Phase 3 tests: search plans, offer matching, trust, adapters (fake transport), search service."""
from __future__ import annotations

import base64
import json
import urllib.parse
from datetime import datetime
from pathlib import Path

import pytest

from config.settings import BRAND_ALIASES_PATH, load_scoring_config
from src.analysis.scoring import Scorer
from src.analysis.supplier_trust import assess_offer_trust
from src.collectors.http_client import ApiAuthError, JsonHttpClient
from src.collectors.supplier_sources import (
    EBAY_SEARCH_URL,
    EBAY_TOKEN_URL,
    RAKUTEN_SEARCH_URL,
    EbayBrowseSource,
    Listing,
    RakutenIchibaSource,
)
from src.discovery.product_normalizer import KeywordDictionary
from src.discovery.supplier_search import build_search_plan
from src.matching.offer_matcher import match_listing
from src.models.enums import (
    AvailabilityStatus,
    RunStatus,
    SupplierType,
    TrustLevel,
    VerificationStatus,
)
from src.models.product import Product
from src.models.supplier import Supplier, SupplierOffer
from src.services.pipeline import AnalysisPipeline
from src.services.supplier_search_service import SourceSpec, SupplierSearchService

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "data" / "sample"
D = KeywordDictionary.load(BRAND_ALIASES_PATH)
DYSON = Product(id=1, brand="Dyson", product_name="Dyson Airwrap Complete", model_number="HS05",
                category="beauty", variant="nickel-copper")
LEGO = Product(id=2, brand="Lego", product_name="LEGO Icons Orchid", model_number="10311",
               gtin="5702017153056", category="toys")


# ---------------------------------------------------------------- search plan

def test_search_plan_staged_and_multilingual():
    en = build_search_plan(LEGO, D, "en", supports_gtin=True)
    assert [(q.step, q.kind) for q in en[:2]] == [(0, "gtin"), (1, "keyword")]
    assert en[0].text == "5702017153056"
    assert [q.step for q in en] == sorted(q.step for q in en)  # specific -> broad
    ja = build_search_plan(DYSON, D, "ja")
    texts = [q.text for q in ja]
    assert "ダイソン HS05" in texts and "ダイソン エアラップ" in texts
    assert all(q.kind == "keyword" for q in ja)  # no GTIN on Rakuten


# ---------------------------------------------------------------- matching

@pytest.mark.parametrize("title,expected", [
    ("Dyson Airwrap Complete HS05 Nickel/Copper NEW", "unverified"),
    ("【国内正規品】ダイソン エアラップ HS05 ニッケル", "unverified"),
    ("Case for Dyson Airwrap HS05 storage bag", "needs_review"),
    ("Dyson HS05 Airwrap Pre-Owned", "needs_review"),
    ("Dyson Airwrap complete long", "needs_review"),   # no model number
    ("Shark FlexStyle air styler", None),
])
def test_offer_matching(title, expected):
    m = match_listing(title, DYSON, D)
    assert (m.status.value if m else None) == expected


def test_numeric_model_alone_needs_review_and_bundle_flagged():
    assert match_listing("10311 Brand new box", LEGO, D).status == VerificationStatus.NEEDS_REVIEW
    assert match_listing("LEGO 10311 Orchid", LEGO, D).status == VerificationStatus.UNVERIFIED
    assert match_listing("Lego Icons Orchid 10311 lot of 2", LEGO, D).status == VerificationStatus.NEEDS_REVIEW


def test_variant_mismatch_lowers_confidence():
    full = match_listing("Dyson Airwrap HS05 nickel copper", DYSON, D)
    partial = match_listing("Dyson Airwrap HS05", DYSON, D)
    assert full.confidence > partial.confidence


# ---------------------------------------------------------------- trust

def _offer(**kw):
    base = dict(product_id=1, supplier_name="S", foreign_price=10, currency="USD",
                availability_status=AvailabilityStatus.IN_STOCK, ships_to_kr=True,
                seller_rating=99.0, seller_rating_count=500)
    base.update(kw)
    return SupplierOffer(**base)


def test_trust_exclusions():
    assert not assess_offer_trust(_offer()).excluded
    assert assess_offer_trust(_offer(availability_status=AvailabilityStatus.OUT_OF_STOCK)).excluded
    assert assess_offer_trust(_offer(item_condition="Used")).excluded
    blocked = Supplier(name="S", trust_level=TrustLevel.BLOCKED)
    assert assess_offer_trust(_offer(), blocked).exclusion_reason == "사용자가 차단한 공급처"
    low = assess_offer_trust(_offer(seller_rating=70, seller_rating_count=200, ships_to_kr=None,
                                    shipping_cost_known=False))
    assert low.excluded and "신뢰도" in low.exclusion_reason


def test_trust_scores_supplier_type_and_user_trust():
    official = assess_offer_trust(_offer(supplier_type=SupplierType.OFFICIAL_STORE))
    market = assess_offer_trust(_offer(supplier_type=SupplierType.MARKETPLACE_SELLER))
    trusted = assess_offer_trust(_offer(supplier_type=SupplierType.MARKETPLACE_SELLER),
                                 Supplier(name="S", trust_level=TrustLevel.TRUSTED))
    assert official.score > market.score and trusted.score > market.score


# ---------------------------------------------------------------- adapters (fake transport)

class Recorder:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body))
        return self.responder(method, url, headers, body)


def _client(t, **kw):
    return JsonHttpClient(transport=t, sleep=lambda s: None, min_interval_s=0, **kw)


def test_ebay_token_and_search_requests():
    def respond(method, url, headers, body):
        if url == EBAY_TOKEN_URL:
            return 200, json.dumps({"access_token": "TOKEN", "expires_in": 7200})
        return 200, json.dumps({"itemSummaries": [{
            "itemId": "v1|1|0", "title": "LEGO Icons Orchid 10311",
            "price": {"value": "39.99", "currency": "USD"},
            "itemWebUrl": "https://www.ebay.com/itm/1",
            "seller": {"username": "bricks", "feedbackPercentage": "99.6", "feedbackScore": 1234},
            "condition": "New",
            "shippingOptions": [{"shippingCost": {"value": "12.50", "currency": "USD"}}],
            "itemLocation": {"country": "US"},
        }]})

    t = Recorder(respond)
    src = EbayBrowseSource("cid", "csecret", client=_client(t), clock=lambda: 1000.0)
    a = src.search("5702017153056", kind="gtin")
    src.search("Lego 10311")
    token_calls = [c for c in t.calls if c[1] == EBAY_TOKEN_URL]
    assert len(token_calls) == 1  # cached
    _, _, h, body = token_calls[0]
    assert h["Authorization"] == "Basic " + base64.b64encode(b"cid:csecret").decode()
    assert h["Content-Type"] == "application/x-www-form-urlencoded"
    assert b"grant_type=client_credentials" in body

    search_urls = [c for c in t.calls if c[1].startswith(EBAY_SEARCH_URL)]
    q1 = urllib.parse.parse_qs(urllib.parse.urlparse(search_urls[0][1]).query)
    q2 = urllib.parse.parse_qs(urllib.parse.urlparse(search_urls[1][1]).query)
    assert "gtin" in q1 and "q" not in q1 and q2["q"] == ["Lego 10311"]
    assert "deliveryCountry:KR" in q1["filter"][0] and "conditions:{NEW}" in q1["filter"][0]
    assert search_urls[0][2]["Authorization"] == "Bearer TOKEN"
    assert search_urls[0][2]["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"
    l = a[0]
    assert (l.price, l.currency, l.shipping_cost, l.seller_rating, l.seller_rating_count) == (39.99, "USD", 12.5, 99.6, 1234)


def test_rakuten_parse_and_not_found():
    def respond(method, url, headers, body):
        assert headers["accessKey"] == "AK"
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if q["keyword"] == ["nothing"]:
            return 404, '{"error":"not_found","error_description":"not found"}'
        assert q["formatVersion"] == ["2"] and q["applicationId"] == ["APP"]
        return 200, json.dumps({"items": [
            {"itemName": "ダイソン エアラップ HS05", "itemPrice": 59800, "itemUrl": "https://item.rakuten.co.jp/x/1",
             "itemCode": "x:1", "shopName": "ショップX", "availability": 1, "postageFlag": 1,
             "shipOverseasFlag": 0, "reviewAverage": 4.5, "reviewCount": 80, "taxFlag": 0},
        ]})

    src = RakutenIchibaSource("APP", "AK", client=_client(Recorder(respond)))
    [l] = src.search("ダイソン HS05")
    assert l.currency == "JPY" and l.shipping_cost is None and l.ships_to_kr is False
    assert l.availability == AvailabilityStatus.IN_STOCK and l.seller_rating == 90.0
    assert src.search("nothing") == []
    assert src.search("x", kind="gtin") == []  # GTIN not supported -> no call


def test_http_auth_error_no_retry_no_secret():
    t = Recorder(lambda *a: (401, '{"error":"invalid_client"}'))
    src = RakutenIchibaSource("APP-SECRET-ID", "ACCESS-SECRET", client=_client(t))
    with pytest.raises(ApiAuthError) as exc:
        src.search("x y")
    assert len(t.calls) == 1
    assert "ACCESS-SECRET" not in str(exc.value) and "APP-SECRET-ID" not in str(exc.value)


# ---------------------------------------------------------------- service

class FakeSource:
    lang = "en"
    supports_gtin = True
    is_domestic = False

    def __init__(self, results, error=None, max_calls=50):
        self.results = results
        self.error = error
        self.queries = []
        self.client = _client(lambda *a: (200, "{}"), max_calls=max_calls)

    def search(self, text, kind="keyword"):
        self.client.calls_made += 1
        self.queries.append((text, kind))
        if self.error:
            raise self.error
        return [Listing(**l) for l in self.results.get(text, [])]


def _listing(title, price, url, **kw):
    base = dict(title=title, price=price, currency="USD", url=url, seller_name="eBay: bricks",
                supplier_country="US", shipping_cost=10.0, shipping_currency="USD",
                ships_to_kr=True, seller_rating=99.5, seller_rating_count=5000, condition="New")
    base.update(kw)
    return base


@pytest.fixture
def pipe(db, converter):
    p = AnalysisPipeline(db, Scorer(load_scoring_config()), converter, dictionary=D)
    p.run_from_directory(SAMPLE_DIR)
    return p


def _service(pipe, source, name="Fake eBay"):
    spec = SourceSpec(name, "US", False, "https://example.invalid/terms", "test", lambda: source)
    return SupplierSearchService(pipe, [spec], max_queries_per_product=3,
                                 now=lambda: datetime(2026, 10, 16, 12, 0))


def test_search_records_offers_and_picks_eligible_offer(pipe):
    lego = pipe.products.find_by_model_number("10311")[0]
    src = FakeSource({"5702017153056": [
        _listing("LEGO Icons Orchid 10311", 15.00, "https://ebay/1"),
        _listing("Case for LEGO 10311 display box", 5.0, "https://ebay/2"),  # cheap accessory
    ]})
    [out] = _service(pipe, src).search_all(product_ids=[lego.id])
    assert out.status == RunStatus.SUCCESS
    assert src.queries[0] == ("5702017153056", "gtin") and len(src.queries) == 1  # stopped: strong match
    assert (out.matched_auto, out.matched_review) == (1, 1)

    opp = pipe.opportunities.latest_for_product(lego.id)
    assert opp.score_basis["supplier_offer"]["url"] == "https://ebay/1"  # not the cheaper accessory
    assert "supplier_offer_unverified" in opp.risk_flags
    review = [c for c in opp.score_basis["supplier_candidates"] if c["url"] == "https://ebay/2"][0]
    assert not review["eligible"]


def test_research_keeps_history_and_user_decision(pipe):
    lego = pipe.products.find_by_model_number("10311")[0]
    url = "https://ebay/1"
    svc = _service(pipe, FakeSource({"5702017153056": [_listing("LEGO 10311 Orchid", 39.99, url)]}))
    svc.search_all(product_ids=[lego.id])
    offer = pipe.supplier_offers.latest_for_listing(lego.id, "eBay: bricks", url)
    pipe.supplier_offers.set_status_for_listing(offer.id, VerificationStatus.VERIFIED)

    [same] = svc.search_all(product_ids=[lego.id])
    assert same.refreshed == 1 and same.new_observations == 0
    svc2 = _service(pipe, FakeSource({"5702017153056": [_listing("LEGO 10311 Orchid", 35.00, url)]}))
    [changed] = svc2.search_all(product_ids=[lego.id])
    assert changed.price_changes == 1
    rows = [o for o in pipe.supplier_offers.list_by_product(lego.id) if o.product_url == url]
    assert [r.foreign_price for r in rows] == [39.99, 35.0]  # history kept
    assert rows[-1].verification_status == VerificationStatus.VERIFIED  # decision carried over
    opp = pipe.opportunities.latest_for_product(lego.id)
    assert "supplier_offer_unverified" not in opp.risk_flags


def test_rejected_offer_never_used(pipe):
    lego = pipe.products.find_by_model_number("10311")[0]
    url = "https://ebay/1"
    svc = _service(pipe, FakeSource({"5702017153056": [_listing("LEGO 10311 Orchid", 1.0, url)]}))
    svc.search_all(product_ids=[lego.id])
    offer = pipe.supplier_offers.latest_for_listing(lego.id, "eBay: bricks", url)
    pipe.supplier_offers.set_status_for_listing(offer.id, VerificationStatus.REJECTED)
    svc.search_all(product_ids=[lego.id])
    assert pipe.opportunities.latest_for_product(lego.id).score_basis["supplier_offer"]["url"] != url


def test_blocked_supplier_excluded(pipe):
    lego = pipe.products.find_by_model_number("10311")[0]
    _service(pipe, FakeSource({"5702017153056": [_listing("LEGO 10311 Orchid", 1.0, "https://ebay/1")]})) \
        .search_all(product_ids=[lego.id])
    sup = pipe.suppliers.get_by_name("eBay: bricks")
    pipe.suppliers.update(sup.model_copy(update={"trust_level": TrustLevel.BLOCKED}))
    pipe.reanalyze()
    assert pipe.opportunities.latest_for_product(lego.id).score_basis["supplier_offer"]["supplier_name"] != "eBay: bricks"


def test_skipped_and_failing_sources_do_not_break(pipe):
    before = len(pipe.opportunities.list_latest())
    skip = SourceSpec("No Key", "US", False, "", "", None, "키 없음")
    bad_src = FakeSource({}, error=ApiAuthError(403, "forbidden"))
    bad = SourceSpec("Bad", "US", False, "", "", lambda: bad_src)
    outs = SupplierSearchService(pipe, [skip, bad]).search_all()
    assert outs[0].status == RunStatus.SKIPPED
    assert outs[1].status == RunStatus.FAILED and len(bad_src.queries) == 1  # stopped at auth error
    assert pipe.sources.get_by_name("Bad").last_error
    assert len(pipe.opportunities.list_latest()) == before


def test_call_cap_respected(pipe):
    src = FakeSource({}, max_calls=2)
    [out] = _service(pipe, src).search_all()
    assert len(src.queries) == 2 and any("호출 상한" in e for e in out.errors)


def test_domestic_source_and_shipping_currency(pipe, converter):
    dyson = pipe.products.find_by_model_number("HS05")[0]

    class Dom(FakeSource):
        lang = "ko"
        supports_gtin = False
        is_domestic = True

    src = Dom({"HS05": [dict(title="다이슨 에어랩 HS05 니켈 코퍼", price=699000, currency="KRW",
                             url="https://shop/1", seller_name="네이버쇼핑: 몰A", supplier_country="KR")]})
    spec = SourceSpec("Fake Naver", "KR", True, "", "", lambda: src)
    SupplierSearchService(pipe, [spec]).search_all(product_ids=[dyson.id])
    d = pipe.domestic_offers.latest_for_listing(dyson.id, "네이버쇼핑: 몰A", "https://shop/1")
    assert d.verification_status == VerificationStatus.UNVERIFIED and not d.shipping_cost_known
    opp = pipe.opportunities.latest_for_product(dyson.id)
    assert "domestic_shipping_unknown" in opp.risk_flags and "domestic_offer_unverified" in opp.risk_flags

    # Shipping quoted in a different currency is converted, not added raw.
    o = SupplierOffer(product_id=dyson.id, supplier_name="X", foreign_price=100, currency="USD",
                      shipping_cost=10, shipping_currency="EUR")
    assert pipe._shipping_in_offer_currency(o) == pytest.approx(10 * 1490 / 1380)



def test_product_without_eligible_offer_is_marked_not_evaluable(pipe):
    dyson = pipe.products.find_by_model_number("HS05")[0]
    for o in pipe.supplier_offers.list_by_product(dyson.id):
        pipe.supplier_offers.set_status_for_listing(o.id, VerificationStatus.NEEDS_REVIEW)
    pipe.reanalyze()
    opp = pipe.opportunities.latest_for_product(dyson.id)
    assert opp.total_score == 0 and "no_eligible_supplier_offer" in opp.risk_flags
    assert opp.score_basis["not_evaluable"] and opp.estimated_margin_pct is None
    assert opp.score_basis["supplier_candidates"][0]["excluded_reason"] == "동일 상품 여부 검토 필요"
