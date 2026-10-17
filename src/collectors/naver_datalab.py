"""Naver DataLab adapters via NAVER API HUB (PRD 4.1, 4.4).

NAVER API HUB (NAVER Cloud Platform) replaced the developers.naver.com
DataLab endpoints in 2026. Endpoints implemented as documented at
https://api.ncloud-docs.com/docs/naver-api-hub-overview (search trend:
.../naver-api-hub-search-trend, shopping insight:
.../naver-api-hub-shopping-insight-keywords):

    POST https://naverapihub.apigw.ntruss.com/search-trend/v1/search
        startDate, endDate (yyyy-mm-dd), timeUnit (date|week|month),
        keywordGroups: max 5 x {groupName, keywords: max 20}
    POST https://naverapihub.apigw.ntruss.com/shopping/v1/category/keywords
        startDate, endDate, timeUnit, category (shopping category code),
        keyword: max 5 x {name, param: [one keyword]}

Auth: headers X-NCP-APIGW-API-KEY-ID (Client ID) / X-NCP-APIGW-API-KEY
(Client Secret) issued for a NAVER API HUB Application in the NCP console.
401 = bad credentials or the API is not enabled for the Application,
429 = daily allowance exceeded.

``ratio`` values are RELATIVE (largest value in the response = 100); they are
search / click interest, not sales. To keep keywords comparable across
requests, the search-trend adapter puts an anchor keyword in every request
and scales on a log ratio: demand_index = 50 + 25*log10(keyword_recent / anchor_recent)
(clamped 0..100; see anchor_scaled_index).

Credentials are only ever placed in request headers; they are never logged,
stored in the DB, or included in error messages.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from ..analysis.demand_analysis import compute_growth
from ..models.demand import DemandSignal
from ..models.enums import Confidence, SignalType

API_HUB_BASE_URL = "https://naverapihub.apigw.ntruss.com"
SEARCH_URL = f"{API_HUB_BASE_URL}/search-trend/v1/search"
SHOPPING_KEYWORDS_URL = f"{API_HUB_BASE_URL}/shopping/v1/category/keywords"
SEARCH_DOC_URL = "https://api.ncloud-docs.com/docs/naver-api-hub-search-trend"
SHOPPING_DOC_URL = "https://api.ncloud-docs.com/docs/naver-api-hub-shopping-insight-keywords"

MAX_GROUPS_PER_REQUEST = 5
WEEKS_COMPARED = 4  # PRD 7.3: recent 4 weeks vs previous 4 weeks
ANCHOR_SCALE = 50.0      # index of a keyword exactly as popular as the anchor
ANCHOR_LOG_SLOPE = 25.0  # points per 10x difference from the anchor


def anchor_scaled_index(keyword_recent: float, anchor_recent: float) -> float:
    """Map keyword/anchor popularity onto 0..100 on a log scale.

    Real responses show keyword levels can be 10x+ the anchor (e.g. a product
    at ~60 vs. "해외직구" at ~5), so a linear ratio saturates at 100 for
    everything. 50 + 25*log10(ratio) keeps ordering comparable across
    requests: 1x -> 50, 10x -> 75, 100x -> 100, 0.1x -> 25, 0.01x -> 0.
    """
    if keyword_recent <= 0 or anchor_recent <= 0:
        return 0.0
    import math

    return max(0.0, min(100.0, ANCHOR_SCALE + ANCHOR_LOG_SLOPE * math.log10(keyword_recent / anchor_recent)))

# transport(url, headers, body, timeout) -> (http_status, response_text)
Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, str]]


class NaverApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Naver API HTTP {status}: {message}")
        self.status = status


class NaverAuthError(NaverApiError):
    """401/403: bad credentials or the API is not enabled for the Application."""


def urllib_transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _error_message(text: str) -> str:
    """Short error text from the three documented API HUB error shapes."""
    try:
        data = json.loads(text)
        gateway = data.get("error")
        if isinstance(gateway, dict):  # API Gateway: {"error": {errorCode, message, details}}
            parts = [gateway.get("errorCode"), gateway.get("message"), gateway.get("details")]
            return " ".join(str(p) for p in parts if p)[:200] or text[:200]
        if "errMsg" in data:  # Search Trend / Shopping Insight body validation
            return f"{data.get('errMsg')} ({data.get('errId', '')})".strip()[:200]
        code = data.get("errorCode", "")  # Search API / legacy flat shape
        msg = data.get("errorMessage", "")
        return f"{code} {msg}".strip() or text[:200]
    except (ValueError, AttributeError):
        return text[:200]


class NaverDataLabClient:
    """Minimal HTTP client with call cap, pacing and bounded retries."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        transport: Optional[Transport] = None,
        max_calls: int = 50,
        min_interval_s: float = 0.2,
        max_retries: int = 2,
        timeout_s: float = 15.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("Naver client id/secret are required")
        self._headers = {
            "X-NCP-APIGW-API-KEY-ID": client_id,
            "X-NCP-APIGW-API-KEY": client_secret,
            "Content-Type": "application/json",
        }
        self._transport = transport or urllib_transport
        self.max_calls = max_calls
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.timeout_s = timeout_s
        self._sleep = sleep
        self.calls_made = 0
        self._last_call: Optional[float] = None

    @property
    def calls_remaining(self) -> int:
        return max(0, self.max_calls - self.calls_made)

    def post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        attempt = 0
        while True:
            if self.calls_made >= self.max_calls:
                raise NaverApiError(0, f"run call limit reached ({self.max_calls})")
            if self._last_call is not None:
                wait = self.min_interval_s - (time.monotonic() - self._last_call)
                if wait > 0:
                    self._sleep(wait)
            self.calls_made += 1
            self._last_call = time.monotonic()
            try:
                status, text = self._transport(url, self._headers, body, self.timeout_s)
            except OSError as exc:  # network error
                status, text = -1, str(exc)

            if status == 200:
                try:
                    return json.loads(text)
                except ValueError as exc:
                    raise NaverApiError(status, f"invalid JSON response: {exc}") from exc
            if status in (401, 403):
                raise NaverAuthError(status, _error_message(text))
            retryable = status == -1 or status == 429 or status >= 500
            if retryable and attempt < self.max_retries:
                attempt += 1
                self._sleep(1.0 * (2 ** (attempt - 1)))
                continue
            raise NaverApiError(status, _error_message(text))

    def search_trend(
        self, start: date, end: date, time_unit: str, groups: list[tuple[str, list[str]]]
    ) -> dict[str, Any]:
        if not 1 <= len(groups) <= MAX_GROUPS_PER_REQUEST:
            raise ValueError("keywordGroups must contain 1..5 groups")
        payload = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "timeUnit": time_unit,
            "keywordGroups": [
                {"groupName": name, "keywords": kws[:20]} for name, kws in groups
            ],
        }
        return self.post(SEARCH_URL, payload)

    def shopping_keywords(
        self, start: date, end: date, time_unit: str, category: str, keywords: list[str]
    ) -> dict[str, Any]:
        if not 1 <= len(keywords) <= MAX_GROUPS_PER_REQUEST:
            raise ValueError("keyword must contain 1..5 entries")
        if not str(category).strip():
            raise ValueError("category code is required")
        payload = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "timeUnit": time_unit,
            "category": str(category).strip(),
            "keyword": [{"name": kw, "param": [kw]} for kw in keywords],
        }
        return self.post(SHOPPING_KEYWORDS_URL, payload)


# ---------------------------------------------------------------------- analysis


def comparison_window(today: Optional[date] = None, weeks: int = WEEKS_COMPARED) -> tuple[date, date]:
    """Two full ISO weeks blocks (Mon..Sun) ending on the last completed Sunday."""
    today = today or date.today()
    last_sunday = today - timedelta(days=today.weekday() + 1)
    start = last_sunday - timedelta(days=7 * 2 * weeks - 1)
    return start, last_sunday


@dataclass(frozen=True)
class SeriesStats:
    recent_mean: Optional[float]
    previous_mean: Optional[float]
    growth_pct: Optional[float]
    points: int


def series_stats(data: list[dict[str, Any]], window_start: date, weeks: int = WEEKS_COMPARED) -> SeriesStats:
    """Split a weekly ratio series into previous/recent blocks and compute growth."""
    split = window_start + timedelta(days=7 * weeks)
    recent: list[float] = []
    previous: list[float] = []
    for point in data or []:
        try:
            period = datetime.strptime(str(point["period"]), "%Y-%m-%d").date()
            ratio = float(point["ratio"])
        except (KeyError, ValueError, TypeError):
            continue
        (recent if period >= split else previous).append(ratio)
    recent_mean = sum(recent) / len(recent) if recent else None
    previous_mean = sum(previous) / len(previous) if previous else None
    growth = (
        compute_growth(recent_mean, previous_mean).growth_pct
        if recent_mean is not None and previous_mean is not None
        else None
    )
    return SeriesStats(recent_mean, previous_mean, growth, len(recent) + len(previous))


@dataclass
class BatchResult:
    signals: list[DemandSignal] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Keywords the API answered for but with no data in the window (too little
    # search/click volume). A valid empty result, not an error.
    no_data: list[str] = field(default_factory=list)


def signals_from_search_response(
    response: dict[str, Any],
    keywords: list[str],
    anchor: str,
    window: tuple[date, date],
    source_id: Optional[int],
    observed_at: Optional[datetime] = None,
) -> BatchResult:
    """Convert a search-trend response into anchor-scaled DemandSignals."""
    out = BatchResult()
    by_title = {str(r.get("title")): r for r in response.get("results", [])}
    anchor_stats = series_stats(by_title.get(anchor, {}).get("data", []), window[0])
    if not anchor_stats.recent_mean:
        out.errors.append(f"기준 키워드 '{anchor}'의 최근 지수가 0이거나 없음 — 배치 스케일링 불가")
        return out
    for kw in keywords:
        result = by_title.get(kw)
        if result is None:
            out.errors.append(f"'{kw}': 응답에 결과 없음")
            continue
        stats = series_stats(result.get("data", []), window[0])
        if stats.recent_mean is None:
            out.no_data.append(kw)
            continue
        index = anchor_scaled_index(stats.recent_mean, anchor_stats.recent_mean)
        out.signals.append(
            DemandSignal(
                keyword=kw,
                country="KR",
                source_id=source_id,
                observed_at=observed_at or datetime.now(),
                period_start=datetime.combine(window[0], datetime.min.time()),
                period_end=datetime.combine(window[1], datetime.min.time()),
                demand_index=round(index, 2),
                growth_pct=stats.growth_pct,
                signal_type=SignalType.SEARCH_INTEREST,
                confidence=Confidence.UNVERIFIED,
                raw_reference=(
                    f"naver_datalab_search week anchor='{anchor}' "
                    f"recent4={stats.recent_mean:.2f} prev4={_fmt(stats.previous_mean)} "
                    f"anchor_recent4={anchor_stats.recent_mean:.2f} points={stats.points}"
                ),
            )
        )
    return out


def signals_from_shopping_response(
    response: dict[str, Any],
    keywords: list[str],
    category: str,
    window: tuple[date, date],
    source_id: Optional[int],
    observed_at: Optional[datetime] = None,
) -> BatchResult:
    """Convert a shopping-insight keyword response into DemandSignals.

    The index is relative to the largest value within this request only
    (no cross-request anchor is available inside a shopping category).
    """
    out = BatchResult()
    by_title = {str(r.get("title")): r for r in response.get("results", [])}
    for kw in keywords:
        result = by_title.get(kw)
        if result is None:
            out.errors.append(f"'{kw}': 응답에 결과 없음")
            continue
        stats = series_stats(result.get("data", []), window[0])
        if stats.recent_mean is None:
            out.no_data.append(kw)
            continue
        out.signals.append(
            DemandSignal(
                keyword=kw,
                category=category,
                country="KR",
                source_id=source_id,
                observed_at=observed_at or datetime.now(),
                period_start=datetime.combine(window[0], datetime.min.time()),
                period_end=datetime.combine(window[1], datetime.min.time()),
                demand_index=round(max(0.0, min(100.0, stats.recent_mean)), 2),
                growth_pct=stats.growth_pct,
                signal_type=SignalType.SHOPPING_INTEREST,
                confidence=Confidence.UNVERIFIED,
                raw_reference=(
                    f"naver_shopping_insight week category={category} batch-relative "
                    f"recent4={stats.recent_mean:.2f} prev4={_fmt(stats.previous_mean)} points={stats.points}"
                ),
            )
        )
    return out


def _fmt(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.2f}"
