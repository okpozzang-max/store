"""Demand collection service: runs Naver DataLab adapters (PRD 4.1, 4.4, 6.7).

Responsibilities:
    - register/look up the data source rows (metadata only, never secrets)
    - skip cleanly (RunStatus.SKIPPED) when credentials are missing or the
      source is disabled, so CSV-based analysis keeps working
    - batch keywords to the documented request limits and the per-run call cap
    - isolate failures per batch; stop early on auth errors (401/403)
    - record a collection_runs row and last success/error on the source
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Optional

import yaml

from ..collectors.naver_datalab import (
    MAX_GROUPS_PER_REQUEST,
    SEARCH_DOC_URL,
    SHOPPING_DOC_URL,
    NaverApiError,
    NaverAuthError,
    NaverDataLabClient,
    comparison_window,
    signals_from_search_response,
    signals_from_shopping_response,
)
from ..discovery.product_normalizer import normalize_text
from ..models.demand import DataSource
from ..models.enums import RunStatus
from .pipeline import AnalysisPipeline

NAVER_SEARCH_SOURCE = "네이버 데이터랩 검색어 트렌드"
NAVER_SHOPPING_SOURCE = "네이버 쇼핑인사이트"
_LICENSE_NOTE = (
    "NAVER API HUB(네이버 클라우드 플랫폼) — 이용약관 준수, 호출 한도(429)는 콘솔에서 확인. "
    "값은 상대 지수(최대=100)이며 판매량이 아님."
)

ClientFactory = Callable[[], NaverDataLabClient]


@dataclass
class CollectionOutcome:
    source_name: str
    status: RunStatus
    signals_collected: int = 0
    records_failed: int = 0
    calls_made: int = 0
    keywords_requested: int = 0
    errors: list[str] = field(default_factory=list)
    message: str = ""
    no_data_keywords: list[str] = field(default_factory=list)


def load_category_codes(path: str | Path) -> dict[str, str]:
    """Product category -> Naver shopping category code (user-maintained)."""
    p = Path(path)
    if not p.is_file():
        return {}
    with open(p, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return {
        normalize_text(str(k)): str(v).strip()
        for k, v in (data.get("categories") or {}).items()
        if str(v).strip()
    }


class DemandCollectionService:
    def __init__(
        self,
        pipeline: AnalysisPipeline,
        client_factory: Optional[ClientFactory],
        anchor_keyword: str = "해외직구",
        category_codes: Optional[dict[str, str]] = None,
        today: Optional[date] = None,
        shopping_client_factory: Optional[ClientFactory] = None,
    ) -> None:
        """``client_factory`` is None when credentials are not configured.

        ``shopping_client_factory`` is for a separate Shopping Insight
        Application; when None, ``client_factory`` is used for both.
        """
        self.pipeline = pipeline
        self.client_factory = client_factory
        self.shopping_client_factory = shopping_client_factory or client_factory
        self.anchor = anchor_keyword
        self.category_codes = category_codes or {}
        self.today = today
        self.search_source_id = pipeline.sources.ensure(
            DataSource(
                name=NAVER_SEARCH_SOURCE,
                source_type="search_trend",
                country="KR",
                access_method="api",
                terms_url=SEARCH_DOC_URL,
                license_notes=_LICENSE_NOTE,
            )
        )
        self.shopping_source_id = pipeline.sources.ensure(
            DataSource(
                name=NAVER_SHOPPING_SOURCE,
                source_type="shopping_insight",
                country="KR",
                access_method="api",
                terms_url=SHOPPING_DOC_URL,
                license_notes=_LICENSE_NOTE + " 쇼핑 분야 코드 필요(config/naver_categories.yaml).",
            )
        )

    # ------------------------------------------------------------------ helpers

    def _unique_query_keywords(self) -> list[tuple[int, str]]:
        seen: set[str] = set()
        out = []
        for product_id, kw in self.pipeline.query_keywords():
            norm = normalize_text(kw)
            if norm and norm not in seen and norm != normalize_text(self.anchor):
                seen.add(norm)
                out.append((product_id, kw))
        return out

    def _precheck(
        self, source_id: int, name: str, factory: Optional[ClientFactory], env_hint: str
    ) -> Optional[CollectionOutcome]:
        source = self.pipeline.sources.get(source_id)
        reason = None
        if source is not None and not source.enabled:
            reason = "데이터 소스가 비활성화되어 있어 건너뜀"
        elif factory is None:
            reason = f"{env_hint}(NAVER API HUB 키) 환경변수가 설정되지 않아 건너뜀"
        if reason is None:
            return None
        run_id = self.pipeline.runs.start(source_id)
        self.pipeline.runs.finish(run_id, RunStatus.SKIPPED, error_summary=reason)
        return CollectionOutcome(source_name=name, status=RunStatus.SKIPPED, message=reason)

    def _finish(self, source_id: int, outcome: CollectionOutcome, run_id: int) -> CollectionOutcome:
        if outcome.signals_collected and not outcome.errors:
            outcome.status = RunStatus.SUCCESS
        elif outcome.signals_collected:
            outcome.status = RunStatus.PARTIAL
        elif outcome.errors:
            outcome.status = RunStatus.FAILED
        else:
            outcome.status = RunStatus.SUCCESS
        outcome.records_failed = len(outcome.errors)
        summary = list(outcome.errors[:10])
        if outcome.no_data_keywords:
            nd = outcome.no_data_keywords
            note = f"데이터 없음(검색·클릭량 부족) {len(nd)}개: {', '.join(nd[:8])}" + (" 외" if len(nd) > 8 else "")
            outcome.message = (outcome.message + " · " if outcome.message else "") + note
            summary.append(note)
        self.pipeline.runs.finish(
            run_id,
            outcome.status,
            records_collected=outcome.signals_collected,
            records_failed=outcome.records_failed,
            error_summary="; ".join(summary) or None,
        )
        if outcome.status in (RunStatus.SUCCESS, RunStatus.PARTIAL):
            self.pipeline.sources.mark_success(source_id)
        if outcome.errors:
            self.pipeline.sources.mark_error(source_id, "; ".join(outcome.errors[:3]))
        return outcome

    # ------------------------------------------------------------------ search trend

    def collect_search_trend(self, keywords: Optional[list[str]] = None) -> CollectionOutcome:
        skipped = self._precheck(
            self.search_source_id, NAVER_SEARCH_SOURCE, self.client_factory,
            "NAVER_CLIENT_ID / NAVER_CLIENT_SECRET",
        )
        if skipped:
            return skipped
        assert self.client_factory is not None
        client = self.client_factory()
        run_id = self.pipeline.runs.start(self.search_source_id)
        outcome = CollectionOutcome(source_name=NAVER_SEARCH_SOURCE, status=RunStatus.RUNNING)

        kws = keywords if keywords is not None else [kw for _, kw in self._unique_query_keywords()]
        outcome.keywords_requested = len(kws)
        window = comparison_window(self.today)
        per_batch = MAX_GROUPS_PER_REQUEST - 1  # one slot reserved for the anchor
        batches = [kws[i : i + per_batch] for i in range(0, len(kws), per_batch)]

        for i, batch in enumerate(batches):
            if client.calls_remaining <= 0:
                outcome.errors.append(
                    f"호출 상한({client.max_calls}회) 도달 — 키워드 {sum(len(b) for b in batches[i:])}개 미수집"
                )
                break
            groups = [(kw, [kw]) for kw in batch] + [(self.anchor, [self.anchor])]
            try:
                response = client.search_trend(window[0], window[1], "week", groups)
            except NaverAuthError as exc:
                outcome.errors.append(f"인증/권한 오류로 중단: {exc}")
                break
            except (NaverApiError, ValueError) as exc:
                outcome.errors.append(f"배치 {i + 1} 실패: {exc}")
                continue
            result = signals_from_search_response(
                response, batch, self.anchor, window, self.search_source_id
            )
            outcome.errors.extend(result.errors)
            outcome.no_data_keywords.extend(result.no_data)
            for signal in result.signals:
                outcome.signals_collected += int(self.pipeline.store_signal(signal))

        outcome.calls_made = client.calls_made
        return self._finish(self.search_source_id, outcome, run_id)

    # ------------------------------------------------------------------ shopping insight

    def collect_shopping_insight(self) -> CollectionOutcome:
        skipped = self._precheck(
            self.shopping_source_id, NAVER_SHOPPING_SOURCE, self.shopping_client_factory,
            "NAVER_SHOPPING_CLIENT_ID / NAVER_SHOPPING_CLIENT_SECRET (또는 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET)",
        )
        if skipped:
            return skipped
        assert self.shopping_client_factory is not None
        products = {p.id: p for p in self.pipeline.products.list_all()}
        by_code: dict[str, list[str]] = {}
        missing_categories: set[str] = set()
        for product_id, kw in self._unique_query_keywords():
            product = products.get(product_id)
            category = normalize_text(product.category) if product else ""
            code = self.category_codes.get(category)
            if not code:
                missing_categories.add(category or "(없음)")
                continue
            by_code.setdefault(code, []).append(kw)

        if not by_code:
            reason = (
                "쇼핑 분야 코드가 설정된 카테고리가 없어 건너뜀 (config/naver_categories.yaml). "
                f"미설정 카테고리: {', '.join(sorted(missing_categories)) or '-'}"
            )
            run_id = self.pipeline.runs.start(self.shopping_source_id)
            self.pipeline.runs.finish(run_id, RunStatus.SKIPPED, error_summary=reason)
            return CollectionOutcome(NAVER_SHOPPING_SOURCE, RunStatus.SKIPPED, message=reason)

        client = self.shopping_client_factory()
        run_id = self.pipeline.runs.start(self.shopping_source_id)
        outcome = CollectionOutcome(source_name=NAVER_SHOPPING_SOURCE, status=RunStatus.RUNNING)
        outcome.keywords_requested = sum(len(v) for v in by_code.values())
        if missing_categories:
            outcome.message = f"분야 코드 미설정으로 제외된 카테고리: {', '.join(sorted(missing_categories))}"
        window = comparison_window(self.today)
        stop = False
        for code, kws in by_code.items():
            for start in range(0, len(kws), MAX_GROUPS_PER_REQUEST):
                batch = kws[start : start + MAX_GROUPS_PER_REQUEST]
                if client.calls_remaining <= 0:
                    outcome.errors.append(f"호출 상한({client.max_calls}회) 도달")
                    stop = True
                    break
                try:
                    response = client.shopping_keywords(window[0], window[1], "week", code, batch)
                except NaverAuthError as exc:
                    outcome.errors.append(f"인증/권한 오류로 중단: {exc}")
                    stop = True
                    break
                except (NaverApiError, ValueError) as exc:
                    outcome.errors.append(f"분야 {code} 배치 실패: {exc}")
                    continue
                result = signals_from_shopping_response(
                    response, batch, code, window, self.shopping_source_id
                )
                outcome.errors.extend(result.errors)
                outcome.no_data_keywords.extend(result.no_data)
                for signal in result.signals:
                    outcome.signals_collected += int(self.pipeline.store_signal(signal))
            if stop:
                break
        outcome.calls_made = client.calls_made
        return self._finish(self.shopping_source_id, outcome, run_id)

    # ------------------------------------------------------------------ all

    def collect_all(self, reanalyze: bool = True) -> list[CollectionOutcome]:
        """Run every Naver adapter; one failing never prevents the other."""
        outcomes = []
        for fn in (self.collect_search_trend, self.collect_shopping_insight):
            try:
                outcomes.append(fn())
            except Exception as exc:  # noqa: BLE001 - never crash the app
                outcomes.append(
                    CollectionOutcome(fn.__name__, RunStatus.FAILED, errors=[str(exc)])
                )
        if reanalyze and any(o.signals_collected for o in outcomes):
            self.pipeline.reanalyze()
        return outcomes


def build_naver_client_factory(settings, shopping: bool = False) -> Optional[ClientFactory]:
    """Factory from settings; None when credentials are absent.

    ``shopping=True`` uses the Shopping Insight key pair (dedicated pair if set,
    otherwise the shared NAVER_CLIENT_ID / NAVER_CLIENT_SECRET).
    """
    if shopping:
        if not settings.naver_shopping_configured:
            return None
        client_id, client_secret = settings.naver_shopping_client_id, settings.naver_shopping_client_secret
    else:
        if not settings.naver_configured:
            return None
        client_id, client_secret = settings.naver_client_id, settings.naver_client_secret

    def factory() -> NaverDataLabClient:
        return NaverDataLabClient(
            client_id, client_secret, max_calls=settings.naver_max_calls_per_run
        )

    return factory


__all__ = [
    "CollectionOutcome",
    "DemandCollectionService",
    "NAVER_SEARCH_SOURCE",
    "NAVER_SHOPPING_SOURCE",
    "build_naver_client_factory",
    "load_category_codes",
]
