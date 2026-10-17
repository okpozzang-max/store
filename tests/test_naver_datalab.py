"""Naver DataLab adapter + collection service tests (fake transport, no network)."""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path

import pytest

from config.settings import BRAND_ALIASES_PATH, load_scoring_config
from src.analysis.scoring import Scorer
from src.collectors.naver_datalab import (
    SEARCH_URL,
    NaverApiError,
    NaverAuthError,
    NaverDataLabClient,
    anchor_scaled_index,
    comparison_window,
    series_stats,
    signals_from_search_response,
)
from src.discovery.product_normalizer import KeywordDictionary
from src.models.enums import RunStatus, SignalType
from src.services.demand_collection import (
    NAVER_SEARCH_SOURCE,
    DemandCollectionService,
)
from src.services.pipeline import AnalysisPipeline

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "data" / "sample"
TODAY = date(2026, 10, 16)  # Friday -> window ends Sunday 2026-10-11


def weekly(start: date, values: list[float]) -> list[dict]:
    return [{"period": (start + timedelta(days=7 * i)).isoformat(), "ratio": v}
            for i, v in enumerate(values)]


class FakeTransport:
    """Records requests; answers each group with a configured weekly series."""

    def __init__(self, series: dict[str, list[float]], status: int = 200, fail_times: int = 0):
        self.series = series
        self.status = status
        self.fail_times = fail_times
        self.requests: list[tuple[str, dict, dict]] = []

    def __call__(self, url, headers, body, timeout):
        payload = json.loads(body.decode("utf-8"))
        self.requests.append((url, headers, payload))
        if self.fail_times > 0:
            self.fail_times -= 1
            return 500, '{"errorCode":"500","errorMessage":"server"}'
        if self.status != 200:
            return self.status, '{"errorCode":"024","errorMessage":"Scope Status Invalid"}'
        start = date.fromisoformat(payload["startDate"])
        if url == SEARCH_URL:
            names = [g["groupName"] for g in payload["keywordGroups"]]
            key = "keywords"
        else:
            names = [k["name"] for k in payload["keyword"]]
            key = "keyword"
        results = [
            {"title": n, key: [n], "data": weekly(start, self.series.get(n, [1.0] * 8))}
            for n in names
        ]
        return 200, json.dumps({"startDate": payload["startDate"], "results": results})


def make_client(transport, **kw):
    return NaverDataLabClient("id", "secret", transport=transport, sleep=lambda s: None, **kw)


# ---------------------------------------------------------------- pure functions

def test_comparison_window_full_weeks():
    start, end = comparison_window(TODAY)
    assert end == date(2026, 10, 11) and end.weekday() == 6  # Sunday
    assert start.weekday() == 0 and (end - start).days == 55  # 8 full weeks


def test_series_stats_growth():
    start, _ = comparison_window(TODAY)
    stats = series_stats(weekly(start, [10, 10, 10, 10, 15, 15, 15, 15]), start)
    assert stats.previous_mean == 10 and stats.recent_mean == 15
    assert stats.growth_pct == pytest.approx(50.0)


def test_series_stats_zero_baseline_is_insufficient():
    start, _ = comparison_window(TODAY)
    stats = series_stats(weekly(start, [0, 0, 0, 0, 5, 5, 5, 5]), start)
    assert stats.growth_pct is None  # never infinite


def test_anchor_scaling():
    window = comparison_window(TODAY)
    resp = {"results": [
        {"title": "a", "data": weekly(window[0], [20] * 8)},
        {"title": "anchor", "data": weekly(window[0], [40] * 8)},
    ]}
    out = signals_from_search_response(resp, ["a"], "anchor", window, source_id=1)
    assert out.signals[0].demand_index == pytest.approx(50 + 25 * math.log10(0.5), abs=0.01)
    assert out.signals[0].signal_type == SignalType.SEARCH_INTEREST


def test_anchor_scaling_does_not_saturate_for_popular_keywords():
    # Live data: products at ~60 vs anchor "해외직구" at ~5 must not all become 100.
    a = anchor_scaled_index(60, 5)
    b = anchor_scaled_index(30, 5)
    assert 0 < b < a < 100
    assert anchor_scaled_index(5, 5) == pytest.approx(50)
    assert anchor_scaled_index(0, 5) == 0.0


# ---------------------------------------------------------------- client

def test_client_sends_documented_request():
    t = FakeTransport({})
    client = make_client(t)
    start, end = comparison_window(TODAY)
    client.search_trend(start, end, "week", [("x", ["x"])])
    url, headers, payload = t.requests[0]
    assert url == "https://naverapihub.apigw.ntruss.com/search-trend/v1/search"
    assert headers["X-NCP-APIGW-API-KEY-ID"] == "id" and headers["X-NCP-APIGW-API-KEY"] == "secret"
    assert headers["Content-Type"] == "application/json" and "X-Naver-Client-Id" not in headers
    assert payload["timeUnit"] == "week" and payload["keywordGroups"][0]["groupName"] == "x"


def test_client_retries_server_errors_then_succeeds():
    t = FakeTransport({}, fail_times=1)
    client = make_client(t, max_retries=2)
    start, end = comparison_window(TODAY)
    client.search_trend(start, end, "week", [("x", ["x"])])
    assert client.calls_made == 2


def test_client_auth_error_not_retried_and_hides_secret():
    t = FakeTransport({}, status=403)
    client = make_client(t)
    start, end = comparison_window(TODAY)
    with pytest.raises(NaverAuthError) as exc:
        client.search_trend(start, end, "week", [("x", ["x"])])
    assert client.calls_made == 1
    assert exc.value.status == 403 and "secret" not in str(exc.value)


def test_error_message_parses_api_hub_shapes():
    from src.collectors.naver_datalab import _error_message

    gw = '{"error":{"errorCode":"200","message":"Authentication Failed","details":"Invalid authentication information."}}'
    assert "Authentication Failed" in _error_message(gw)
    trend = '{"errMsg":"TypeError: bad","errId":"2026-06-11T17:51:49+09:00/1","body":"{}"}'
    assert "TypeError: bad" in _error_message(trend)
    assert _error_message("not json") == "not json"


def test_shopping_client_uses_api_hub_endpoint():
    t = FakeTransport({})
    client = make_client(t)
    start, end = comparison_window(TODAY)
    client.shopping_keywords(start, end, "week", "50000002", ["a"])
    url, headers, payload = t.requests[0]
    assert url == "https://naverapihub.apigw.ntruss.com/shopping/v1/category/keywords"
    assert "X-NCP-APIGW-API-KEY-ID" in headers and payload["keyword"] == [{"name": "a", "param": ["a"]}]


def test_client_call_cap():
    client = make_client(FakeTransport({}), max_calls=1)
    start, end = comparison_window(TODAY)
    client.search_trend(start, end, "week", [("x", ["x"])])
    with pytest.raises(NaverApiError):
        client.search_trend(start, end, "week", [("x", ["x"])])


def test_group_limit_enforced():
    client = make_client(FakeTransport({}))
    start, end = comparison_window(TODAY)
    with pytest.raises(ValueError):
        client.search_trend(start, end, "week", [(str(i), [str(i)]) for i in range(6)])


# ---------------------------------------------------------------- service

def _pipeline(db, converter):
    return AnalysisPipeline(
        db, Scorer(load_scoring_config()), converter,
        dictionary=KeywordDictionary.load(BRAND_ALIASES_PATH),
    )


def test_service_skips_without_credentials(db, converter):
    pipeline = _pipeline(db, converter)
    pipeline.run_from_directory(SAMPLE_DIR)
    service = DemandCollectionService(pipeline, client_factory=None, today=TODAY)
    outcomes = service.collect_all()
    assert all(o.status == RunStatus.SKIPPED for o in outcomes)
    assert any(r.status == RunStatus.SKIPPED for r in pipeline.runs.list_all())
    # CSV-based analysis still intact.
    assert len(pipeline.opportunities.list_latest()) == 5


def test_service_collects_links_and_rescores(db, converter):
    pipeline = _pipeline(db, converter)
    pipeline.run_from_directory(SAMPLE_DIR)
    before = {o.product_id: o for o in pipeline.opportunities.list_latest()}

    t = FakeTransport({"해외직구": [50] * 8, "Anker A1263": [10, 10, 10, 10, 20, 20, 20, 20]})
    service = DemandCollectionService(
        pipeline, client_factory=lambda: make_client(t), anchor_keyword="해외직구", today=TODAY
    )
    outcome = service.collect_search_trend()
    assert outcome.status in (RunStatus.SUCCESS, RunStatus.PARTIAL)
    assert outcome.signals_collected > 0
    # Every request respects the 5-group limit and includes the anchor.
    for _, _, payload in t.requests:
        names = [g["groupName"] for g in payload["keywordGroups"]]
        assert len(names) <= 5 and "해외직구" in names

    pipeline.reanalyze()
    anker = pipeline.products.find_by_model_number("A1263")[0]
    after = pipeline.opportunities.latest_for_product(anker.id)
    assert after.demand_score > before[anker.id].demand_score  # model-number link -> demand counted
    assert after.growth_score > 0
    sources = {s["source"] for s in after.score_basis["demand"]["signals"]}
    assert NAVER_SEARCH_SOURCE in sources

    # Re-collecting the same window does not duplicate signals.
    count = len(pipeline.demand.list_all())
    service.collect_search_trend()
    assert len(pipeline.demand.list_all()) == count


def test_service_auth_failure_recorded_not_raised(db, converter):
    pipeline = _pipeline(db, converter)
    pipeline.run_from_directory(SAMPLE_DIR)
    service = DemandCollectionService(
        pipeline, client_factory=lambda: make_client(FakeTransport({}, status=403)), today=TODAY
    )
    outcome = service.collect_search_trend()
    assert outcome.status == RunStatus.FAILED
    src = pipeline.sources.get_by_name(NAVER_SEARCH_SOURCE)
    assert src.last_error and "403" in src.last_error
    assert len(pipeline.opportunities.list_latest()) == 5  # existing data untouched


def test_disabled_source_is_skipped(db, converter):
    pipeline = _pipeline(db, converter)
    service = DemandCollectionService(
        pipeline, client_factory=lambda: make_client(FakeTransport({})), today=TODAY
    )
    pipeline.sources.set_enabled(service.search_source_id, False)
    assert service.collect_search_trend().status == RunStatus.SKIPPED


def test_shopping_skipped_without_category_codes(db, converter):
    pipeline = _pipeline(db, converter)
    pipeline.run_from_directory(SAMPLE_DIR)
    service = DemandCollectionService(
        pipeline, client_factory=lambda: make_client(FakeTransport({})), today=TODAY
    )
    assert service.collect_shopping_insight().status == RunStatus.SKIPPED


def test_shopping_collects_with_category_code(db, converter):
    pipeline = _pipeline(db, converter)
    pipeline.run_from_directory(SAMPLE_DIR)
    t = FakeTransport({})
    service = DemandCollectionService(
        pipeline, client_factory=lambda: make_client(t),
        category_codes={"beauty": "50000002"}, today=TODAY,
    )
    outcome = service.collect_shopping_insight()
    assert outcome.signals_collected > 0
    _, _, payload = t.requests[0]
    assert payload["category"] == "50000002"
    assert all(len(k["param"]) == 1 for k in payload["keyword"])


# ---------------------------------------------------------------- shopping credentials

def _settings_with(monkeypatch, **env):
    from config.settings import Settings
    for k in ("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET", "NAVER_SHOPPING_CLIENT_ID", "NAVER_SHOPPING_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings()


def test_shopping_keys_fall_back_to_shared_pair(monkeypatch):
    from src.services.demand_collection import build_naver_client_factory
    s = _settings_with(monkeypatch, NAVER_CLIENT_ID="a", NAVER_CLIENT_SECRET="b")
    assert not s.naver_shopping_has_own_keys and s.naver_shopping_configured
    assert (s.naver_shopping_client_id, s.naver_shopping_client_secret) == ("a", "b")
    assert build_naver_client_factory(s, shopping=True)()._headers["X-NCP-APIGW-API-KEY-ID"] == "a"


def test_dedicated_shopping_keys_are_used_only_for_shopping(monkeypatch):
    from src.services.demand_collection import build_naver_client_factory
    s = _settings_with(monkeypatch, NAVER_CLIENT_ID="a", NAVER_CLIENT_SECRET="b",
                       NAVER_SHOPPING_CLIENT_ID="c", NAVER_SHOPPING_CLIENT_SECRET="d")
    assert s.naver_shopping_has_own_keys
    shop = build_naver_client_factory(s, shopping=True)()._headers
    main = build_naver_client_factory(s)()._headers
    assert shop["X-NCP-APIGW-API-KEY-ID"] == "c" and shop["X-NCP-APIGW-API-KEY"] == "d"
    assert main["X-NCP-APIGW-API-KEY-ID"] == "a" and main["X-NCP-APIGW-API-KEY"] == "b"


def test_half_filled_shopping_pair_is_ignored(monkeypatch):
    s = _settings_with(monkeypatch, NAVER_CLIENT_ID="a", NAVER_CLIENT_SECRET="b", NAVER_SHOPPING_CLIENT_ID="c")
    assert not s.naver_shopping_has_own_keys and s.naver_shopping_client_id == "a"


def test_shopping_only_keys_enable_shopping_not_search(monkeypatch):
    from src.services.demand_collection import build_naver_client_factory
    s = _settings_with(monkeypatch, NAVER_SHOPPING_CLIENT_ID="c", NAVER_SHOPPING_CLIENT_SECRET="d")
    assert build_naver_client_factory(s) is None and build_naver_client_factory(s, shopping=True) is not None


def test_service_uses_separate_client_for_shopping(db, converter):
    pipeline = _pipeline(db, converter)
    pipeline.run_from_directory(SAMPLE_DIR)
    search_t, shop_t = FakeTransport({}), FakeTransport({})
    service = DemandCollectionService(
        pipeline, client_factory=lambda: make_client(search_t),
        shopping_client_factory=lambda: make_client(shop_t),
        category_codes={"beauty": "50000002"}, today=TODAY,
    )
    service.collect_shopping_insight()
    assert shop_t.requests and not search_t.requests
    service.collect_search_trend()
    assert search_t.requests and all(u == SEARCH_URL for u, _, _ in search_t.requests)


def test_keywords_without_data_are_not_errors(db, converter):
    """Live API returns an empty series for low-volume keywords: that is a valid result."""
    pipeline = _pipeline(db, converter)
    pipeline.run_from_directory(SAMPLE_DIR)
    start, _ = comparison_window(TODAY)

    def respond(url, headers, body, timeout):
        payload = json.loads(body.decode("utf-8"))
        res = []
        for g in payload["keywordGroups"]:
            data = weekly(start, [5.0] * 8) if g["groupName"] in ("해외직구", "Anker A1263") else []
            res.append({"title": g["groupName"], "keywords": [g["groupName"]], "data": data})
        return 200, json.dumps({"results": res})

    service = DemandCollectionService(pipeline, client_factory=lambda: make_client(respond), today=TODAY)
    out = service.collect_search_trend()
    assert out.status == RunStatus.SUCCESS and out.errors == []
    assert out.signals_collected == 1 and out.no_data_keywords
    assert "데이터 없음" in out.message
    assert pipeline.sources.get_by_name(NAVER_SEARCH_SOURCE).last_error is None
