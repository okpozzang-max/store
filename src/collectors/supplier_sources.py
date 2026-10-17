"""Overseas supplier offer sources (PRD 4.2, 4.4).

Implemented only against officially documented, credential-based APIs:

eBay Browse API (verified from the OpenAPI spec and OAuth guide on developer.ebay.com)
    token : POST https://api.ebay.com/identity/v1/oauth2/token
            Content-Type: application/x-www-form-urlencoded
            Authorization: Basic base64(client_id:client_secret)
            body grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope
            -> access_token, expires_in (7200 s)
    search: GET https://api.ebay.com/buy/browse/v1/item_summary/search
            q=<keywords> | gtin=<GTIN> (not combined with q), limit 1..200,
            filter=buyingOptions:{FIXED_PRICE},conditions:{NEW},deliveryCountry:KR
            headers: Authorization: Bearer <token>, X-EBAY-C-MARKETPLACE-ID,
                     X-EBAY-C-ENDUSERCTX: contextualLocation=country%3DKR

Rakuten Ichiba Item Search API 2026-07-01 (webservice.rakuten.co.jp/documentation)
    GET https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701
        applicationId (query) + accessKey (header), keyword, hits 1..30,
        availability=1, format=json, formatVersion=2
    Terms: credit display required ("Supported by Rakuten Developers").

Every adapter returns ``Listing`` records; matching to products and trust
assessment happen in the service layer.
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from ..models.enums import AvailabilityStatus, SupplierType
from .http_client import ApiError, JsonHttpClient

EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_SCOPE = "https://api.ebay.com/oauth/api_scope"
EBAY_DOC_URL = "https://developer.ebay.com/api-docs/buy/browse/resources/item_summary/methods/search"

RAKUTEN_SEARCH_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"
RAKUTEN_DOC_URL = "https://webservice.rakuten.co.jp/documentation/ichiba-item-search"
RAKUTEN_TERMS_URL = "https://webservice.rakuten.co.jp/guide/rule"
RAKUTEN_CREDIT_HTML = (
    '<!-- Rakuten Web Services Attribution Snippet FROM HERE -->'
    '<a href="https://developers.rakuten.com/" target="_blank">Supported by Rakuten Developers</a>'
    '<!-- Rakuten Web Services Attribution Snippet TO HERE -->'
)


@dataclass
class Listing:
    """One search result in a source-neutral shape."""

    title: str
    price: float
    currency: str
    url: Optional[str]
    seller_name: str
    external_id: Optional[str] = None
    supplier_country: Optional[str] = None
    supplier_type: SupplierType = SupplierType.MARKETPLACE_SELLER
    shipping_cost: Optional[float] = None       # None => unknown
    shipping_currency: Optional[str] = None
    availability: AvailabilityStatus = AvailabilityStatus.UNKNOWN
    condition: Optional[str] = None
    ships_to_kr: Optional[bool] = None
    seller_rating: Optional[float] = None        # 0..100
    seller_rating_count: Optional[int] = None
    notes: Optional[str] = None


class OfferSource(Protocol):
    source_name: str
    lang: str
    supports_gtin: bool
    is_domestic: bool
    client: JsonHttpClient

    def search(self, text: str, kind: str = "keyword") -> list[Listing]: ...


def _f(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class EbayBrowseSource:
    source_name = "eBay Browse API"
    lang = "en"
    supports_gtin = True
    is_domestic = False
    terms_url = EBAY_DOC_URL
    license_notes = (
        "eBay Developers Program API License Agreement 준수. Application access token "
        "(client credentials) 사용. 검색 결과는 판매자 등록 정보이며 검증 전 데이터."
    )

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        client: Optional[JsonHttpClient] = None,
        marketplace_id: str = "EBAY_US",
        delivery_country: str = "KR",
        limit: int = 20,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("eBay client id/secret are required")
        self._basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self.client = client or JsonHttpClient(min_interval_s=0.3)
        self.marketplace_id = marketplace_id or "EBAY_US"
        self.delivery_country = (delivery_country or "").upper()
        self.limit = max(1, min(200, limit))
        self._clock = clock
        self._token: Optional[str] = None
        self._token_expiry = 0.0

    def _get_token(self) -> str:
        if self._token and self._clock() < self._token_expiry - 60:
            return self._token
        body = f"grant_type=client_credentials&scope={EBAY_SCOPE}".encode()
        data = self.client.request(
            "POST",
            EBAY_TOKEN_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {self._basic}",
            },
            body=body,
        )
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            raise ApiError(200, "token response missing access_token")
        self._token = token
        self._token_expiry = self._clock() + float(data.get("expires_in", 7200))
        return token

    def search(self, text: str, kind: str = "keyword") -> list[Listing]:
        token = self._get_token()
        filters = ["buyingOptions:{FIXED_PRICE}", "conditions:{NEW}"]
        if self.delivery_country:
            filters.append(f"deliveryCountry:{self.delivery_country}")
        params: dict[str, Any] = {"limit": self.limit, "filter": ",".join(filters)}
        params["gtin" if kind == "gtin" else "q"] = text
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace_id,
            "Accept": "application/json",
        }
        if self.delivery_country:
            headers["X-EBAY-C-ENDUSERCTX"] = f"contextualLocation=country%3D{self.delivery_country}"
        data = self.client.request("GET", EBAY_SEARCH_URL, params=params, headers=headers)
        return [l for l in (self._parse(i) for i in (data or {}).get("itemSummaries", []) or []) if l]

    def _parse(self, item: dict[str, Any]) -> Optional[Listing]:
        price = item.get("price") or {}
        value = _f(price.get("value"))
        if not value or value <= 0 or not price.get("currency"):
            return None
        seller = item.get("seller") or {}
        ship_cost = ship_cur = None
        for opt in item.get("shippingOptions") or []:
            cost = opt.get("shippingCost") or {}
            v = _f(cost.get("value"))
            if v is not None:
                ship_cost, ship_cur = v, cost.get("currency") or price.get("currency")
                break
        return Listing(
            title=str(item.get("title") or ""),
            price=value,
            currency=str(price["currency"]).upper(),
            url=item.get("itemWebUrl"),
            seller_name=f"eBay: {seller.get('username') or 'unknown'}",
            external_id=item.get("itemId"),
            supplier_country=(item.get("itemLocation") or {}).get("country"),
            supplier_type=SupplierType.MARKETPLACE_SELLER,
            shipping_cost=ship_cost,
            shipping_currency=ship_cur,
            availability=AvailabilityStatus.UNKNOWN,  # summaries do not state stock
            condition=item.get("condition"),
            ships_to_kr=True if self.delivery_country == "KR" else None,
            seller_rating=_f(seller.get("feedbackPercentage")),
            seller_rating_count=int(seller["feedbackScore"]) if str(seller.get("feedbackScore", "")).isdigit() else None,
        )


class RakutenIchibaSource:
    source_name = "楽天市場 商品検索 API"
    lang = "ja"
    supports_gtin = False
    is_domestic = False
    terms_url = RAKUTEN_TERMS_URL
    license_notes = (
        "Rakuten Web Service 이용약관 준수, 화면에 'Supported by Rakuten Developers' 크레딧 표시 의무. "
        "가격은 JPY·세금 포함 여부(taxFlag) 별도, 리뷰 평점은 상품 리뷰(판매자 평가 아님)."
    )

    def __init__(
        self,
        application_id: str,
        access_key: str,
        client: Optional[JsonHttpClient] = None,
        hits: int = 20,
    ) -> None:
        if not application_id or not access_key:
            raise ValueError("Rakuten application id / access key are required")
        self._app_id = application_id
        self._access_key = access_key
        # Docs warn identical URLs hit repeatedly may be throttled: pace calls.
        self.client = client or JsonHttpClient(min_interval_s=1.0)
        self.hits = max(1, min(30, hits))

    def search(self, text: str, kind: str = "keyword") -> list[Listing]:
        if kind != "keyword":
            return []
        params = {
            "applicationId": self._app_id,
            "keyword": text[:128],
            "hits": self.hits,
            "availability": 1,
            "format": "json",
            "formatVersion": 2,
        }
        try:
            data = self.client.request(
                "GET", RAKUTEN_SEARCH_URL, params=params,
                headers={"accessKey": self._access_key, "Accept": "application/json"},
            )
        except ApiError as exc:
            if exc.status == 404:  # documented "not_found" = no results
                return []
            raise
        return [l for l in (self._parse(i) for i in (data or {}).get("items", []) or []) if l]

    @staticmethod
    def _parse(item: dict[str, Any]) -> Optional[Listing]:
        price = _f(item.get("itemPrice"))
        if not price or price <= 0:
            return None
        postage_included = item.get("postageFlag") == 0
        overseas = item.get("shipOverseasFlag")
        areas = [a.strip().upper() for a in str(item.get("shipOverseasArea") or "").split("/") if a.strip()]
        ships_to_kr = False if overseas == 0 else (True if "KR" in areas else None)
        review_avg = _f(item.get("reviewAverage"))
        notes = []
        if item.get("taxFlag") == 1:
            notes.append("세금 별도 가격")
        if item.get("shopOfTheYearFlag") == 1:
            notes.append("Shop of the Year 수상 점포")
        return Listing(
            title=str(item.get("itemName") or ""),
            price=price,
            currency="JPY",
            url=item.get("itemUrl"),
            seller_name=f"楽天: {item.get('shopName') or item.get('shopCode') or 'unknown'}",
            external_id=item.get("itemCode"),
            supplier_country="JP",
            supplier_type=SupplierType.MARKETPLACE_SELLER,
            shipping_cost=0.0 if postage_included else None,
            shipping_currency="JPY",
            availability=AvailabilityStatus.IN_STOCK if item.get("availability") == 1
            else AvailabilityStatus.OUT_OF_STOCK if item.get("availability") == 0
            else AvailabilityStatus.UNKNOWN,
            ships_to_kr=ships_to_kr,
            seller_rating=review_avg * 20 if review_avg is not None else None,
            seller_rating_count=int(item.get("reviewCount") or 0),
            notes="; ".join(notes) or None,
        )
