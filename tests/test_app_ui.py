"""UI tests via Streamlit AppTest (headless; PRD 12 UI tests)."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = str(Path(__file__).resolve().parent.parent / "app.py")
PAGES = ["대시보드", "수요 탐색", "소싱 후보 목록", "상품 상세", "해외 공급처", "데이터 소스 관리", "작업 및 로그"]
RAW_ENUMS = {"new", "in_review", "approved", "unverified", "partially_verified", "insufficient_data",
             "search_interest", "trusted", "official_store", "source", "conditional", "exclude"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    import streamlit as st

    import config.settings as cs

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ui.db"))
    for var in ("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET", "EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET",
                "RAKUTEN_APPLICATION_ID", "RAKUTEN_ACCESS_KEY"):
        monkeypatch.setenv(var, "")
    monkeypatch.setattr(cs, "settings", cs.Settings())
    st.cache_resource.clear()  # fresh DB per test
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception
    yield at
    st.cache_resource.clear()


def _click(at, label):
    next(b for b in at.button if b.label.startswith(label)).click().run()
    assert not at.exception, at.exception


def _goto(at, page):
    at.sidebar.radio[0].set_value(page).run()
    assert not at.exception, (page, at.exception)


def test_onboarding_when_empty(app):
    assert any("시작하기" in i.value for i in app.info)


def test_all_pages_render_after_import(app):
    _click(app, "샘플 CSV 가져오기")
    for page in PAGES:
        _goto(app, page)
    assert any("가상 샘플" in w.value for w in app.warning)


def test_job_result_persists_across_pages(app):
    _click(app, "샘플 CSV 가져오기")
    _goto(app, "작업 및 로그")
    assert any("신규 상품" in s.value for s in app.success)


def test_naver_without_keys_is_skipped_not_crash(app):
    _click(app, "네이버 수요 수집")
    assert any("건너뜀" in i.value for i in app.info)


def test_supplier_search_without_keys_is_skipped(app):
    _click(app, "해외 공급처 탐색")
    assert sum("건너뜀" in i.value for i in app.info) == 2  # eBay, Rakuten


def test_candidate_list_labels_and_order(app):
    _click(app, "샘플 CSV 가져오기")
    _goto(app, "소싱 후보 목록")
    df = app.dataframe[0].value
    assert "검증 완료" not in set(df["검증"])
    tiers = list(df["판정"])
    assert tiers == sorted(tiers, key=lambda t: ["✅", "⚠️", "⛔"].index(t.split()[0]))
    assert tiers[-1].startswith("⛔")  # loss-makers sink to the bottom
    shown = set(df["검토상태"]) | set(df["신뢰도"]) | set(df["판정"])
    assert not shown & RAW_ENUMS


def test_list_to_detail_navigation(app):
    _click(app, "샘플 CSV 가져오기")
    _goto(app, "소싱 후보 목록")
    target = app.selectbox(key="opp_pick").value
    _click(app, "상세 보기")
    assert app.session_state["page"] == "상품 상세"
    assert app.selectbox(key="detail_product").value == target


def test_manual_link_confirmation(app):
    _click(app, "샘플 CSV 가져오기")
    _goto(app, "수요 탐색")
    before = len([b for b in app.button if b.label == "연결 확정"])
    assert before > 0
    _click(app, "연결 확정")
    assert len([b for b in app.button if b.label == "연결 확정"]) == before - 1


def test_offer_decision_and_sale_price_in_detail(app):
    _click(app, "샘플 CSV 가져오기")
    _goto(app, "상품 상세")
    assert any(b.label == "공급처 제외" for b in app.button)
    next(b for b in app.button if b.label == "국내가 확정").click().run()
    assert not app.exception
    # Own sale price changes the basis of the calculation.
    app.number_input[0].set_value(60000)
    next(b for b in app.button if b.label == "저장 후 재계산").click().run()
    assert not app.exception
    assert app.number_input[0].value == 60000


def test_channel_switch_rescored(app):
    _click(app, "샘플 CSV 가져오기")
    _goto(app, "소싱 후보 목록")
    m1 = list(app.dataframe[0].value["예상 마진"])
    app.sidebar.selectbox[0].set_value("coupang").run()
    assert not app.exception
    m2 = list(app.dataframe[0].value["예상 마진"])
    assert sum(m2) < sum(m1)  # higher fee -> lower margins
