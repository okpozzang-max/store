"""Streamlit UI for Overseas Demand Radar (PRD 10).

Screens: 대시보드 / 수요 탐색 / 소싱 후보 목록 / 상품 상세 / 해외 공급처 / 데이터 소스 관리 / 작업 및 로그.
The UI only orchestrates; collection, calculation and DB access live in the
service and repository layers.

Run with:  streamlit run app.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (  # noqa: E402
    BRAND_ALIASES_PATH,
    NAVER_CATEGORIES_PATH,
    load_scoring_config,
    settings,
)
from src.analysis.scoring import Scorer  # noqa: E402
from src.collectors.supplier_sources import RAKUTEN_CREDIT_HTML, RakutenIchibaSource  # noqa: E402
from src.discovery.product_normalizer import KeywordDictionary, normalize_text  # noqa: E402
from src.models.enums import (  # noqa: E402
    KeywordLinkStatus,
    ReviewStatus,
    RunStatus,
    SupplierType,
    TrustLevel,
    VerificationStatus,
)
from src.services.demand_collection import (  # noqa: E402
    DemandCollectionService,
    build_naver_client_factory,
    load_category_codes,
)
from src.services.pipeline import (  # noqa: E402
    TIER_CONDITIONAL,
    TIER_EXCLUDE,
    TIER_SOURCE,
    AnalysisPipeline,
    decision_tier,
)
from src.services.supplier_search_service import SupplierSearchService, build_source_specs  # noqa: E402
from src.storage.database import Database  # noqa: E402
from src.utils.currency import load_converter_from_csv  # noqa: E402

SAMPLE_DIR = PROJECT_ROOT / "data" / "sample"
SAMPLE_SOURCE = "샘플 데이터 (data/sample)"
STALE_PRICE_DAYS = 14
PAGES = ["대시보드", "수요 탐색", "소싱 후보 목록", "상품 상세", "해외 공급처", "데이터 소스 관리", "작업 및 로그"]

# --------------------------------------------------------------------- labels
# Every enum shown to the user goes through one of these maps (no raw values).

TIER_LABELS = {TIER_SOURCE: "✅ 소싱 검토", TIER_CONDITIONAL: "⚠️ 조건부", TIER_EXCLUDE: "⛔ 제외 권고"}
TIER_HELP = {
    TIER_SOURCE: "시장 최저가로 팔아도 목표 마진을 넘습니다.",
    TIER_CONDITIONAL: "이익은 나지만 목표 마진 미달이거나, 목표 마진을 맞추려면 시장 최저가보다 비싸게 팔아야 합니다.",
    TIER_EXCLUDE: "예상 적자이거나 가격 비교에 쓸 오퍼가 없습니다.",
}
REVIEW_LABELS = {"new": "신규", "in_review": "검토 중", "approved": "소싱 확정", "rejected": "소싱 제외"}
CONFIDENCE_LABELS = {
    "verified": "검증됨", "partially_verified": "일부 확인",
    "unverified": "미검증", "insufficient_data": "데이터 부족",
}
LINK_LABELS = {
    "candidate": "검색어 후보", "auto_linked": "자동 연결", "needs_review": "검토 필요",
    "confirmed": "확정", "rejected": "제외",
}
VERIFY_LABELS = {"verified": "확정", "unverified": "자동 매칭(미검증)", "needs_review": "검토 필요", "rejected": "제외"}
SIGNAL_LABELS = {
    "search_interest": "검색 관심도", "shopping_interest": "쇼핑 클릭 관심도",
    "trend": "트렌드", "import_stats": "수입 통계", "manual": "수동 입력",
}
TRUST_LABELS = {"trusted": "신뢰", "normal": "보통", "blocked": "차단"}
SUPPLIER_TYPE_LABELS = {
    "official_store": "브랜드 공식몰", "authorized_retailer": "공식 리테일러",
    "specialty_retailer": "전문 쇼핑몰", "wholesale": "도매/B2B",
    "marketplace_seller": "마켓플레이스 판매자", "price_comparison": "가격비교", "unknown": "미분류",
}
RUN_LABELS = {"running": "실행 중", "success": "성공", "partial": "일부 성공", "failed": "실패", "skipped": "건너뜀"}
FLAG_LABELS = {
    "negative_margin": "예상 적자",
    "price_uncompetitive": "목표 마진 시 시장가 초과",
    "my_price_above_market": "내 판매가 > 시장 최저가",
    "below_target_margin": "목표 마진 미달",
    "intl_shipping_estimated": "국제배송비 추정치",
    "customs_estimated": "관부가세 추정치",
    "long_lead_time": "배송 기간 김",
    "missing_demand_data": "수요 데이터 없음",
    "insufficient_growth_data": "성장률 데이터 부족",
    "missing_price_data": "가격 데이터 없음",
    "missing_margin_data": "마진 데이터 없음",
    "missing_competition_data": "경쟁 데이터 없음",
    "demand_link_needs_review": "수요 연결 검토 필요",
    "single_demand_source": "단일 수요 출처",
    "supplier_offer_unverified": "공급처 미검증",
    "supplier_shipping_unknown": "해외 배송비 미확인",
    "forwarding_required": "배송대행 필요",
    "ships_to_kr_unknown": "한국 배송 여부 미확인",
    "domestic_offer_unverified": "국내 비교 상품 미검증",
    "domestic_shipping_unknown": "국내 배송비 미확인",
    "no_eligible_supplier_offer": "가격 반영 가능한 해외 오퍼 없음",
    "no_domestic_offer": "국내 비교가 없음",
    "reg:battery": "배터리(항공 제한)",
    "reg:kc_electric": "KC 전기안전",
    "reg:radio_cert": "전파인증",
    "reg:food_contact": "식품 접촉 용기",
    "reg:cosmetic": "화장품 요건",
    "reg:brand_ip": "상표권/병행수입",
}
# Flags that change the buy decision are shown first.
FLAG_PRIORITY = [
    "negative_margin", "no_eligible_supplier_offer", "price_uncompetitive", "reg:battery",
    "below_target_margin", "my_price_above_market", "long_lead_time", "forwarding_required",
    "reg:kc_electric", "reg:radio_cert", "reg:food_contact", "reg:cosmetic", "reg:brand_ip",
    "supplier_offer_unverified", "demand_link_needs_review",
]
COST_LABELS = {
    "product_cost_krw": "상품가",
    "foreign_shipping_krw": "판매처 배송비",
    "payment_fee_krw": "해외 카드 수수료",
    "fx_buffer_krw": "환율 여유분",
    "international_shipping_krw": "국제배송(배송대행)",
    "customs_tax_krw": "관부가세",
    "other_costs_krw": "포장·기타",
    "domestic_shipping_krw": "국내 배송비",
    "platform_fee_krw": "판매 수수료",
    "return_allowance_krw": "반품·CS 충당",
}


def flag_label(f: str) -> str:
    return FLAG_LABELS.get(f, f)


def sorted_flags(flags: list[str]) -> list[str]:
    rank = {f: i for i, f in enumerate(FLAG_PRIORITY)}
    return sorted(flags, key=lambda f: rank.get(f, len(rank)))


def flags_text(flags: list[str]) -> str:
    return ", ".join(flag_label(f) for f in sorted_flags(flags))


def won(v) -> str:
    return "-" if v is None else f"{v:,.0f}원"


def tier_of(opp) -> str:
    dec = opp.score_basis.get("decision") or {}
    return dec.get("tier") or decision_tier(opp.estimated_margin_pct, settings.target_margin_pct, opp.risk_flags)


def is_verified(opp) -> bool:
    """PRD 8.4: verified only when explicitly approved AND nothing is flagged."""
    return opp.review_status == ReviewStatus.APPROVED and not opp.risk_flags


def stale(checked_at: str | None) -> bool:
    if not checked_at:
        return True
    try:
        return datetime.now() - datetime.fromisoformat(checked_at) > timedelta(days=STALE_PRICE_DAYS)
    except ValueError:
        return True


# --------------------------------------------------------------------- wiring


@st.cache_resource
def get_database() -> Database:
    settings.ensure_directories()
    db = Database(settings.database_path)
    db.init_schema()
    return db


def build_pipeline(db: Database, channel: str | None) -> AnalysisPipeline:
    return AnalysisPipeline(
        db,
        Scorer(load_scoring_config()),
        load_converter_from_csv(SAMPLE_DIR / "exchange_rates.csv"),
        target_margin_pct=settings.target_margin_pct,
        dictionary=KeywordDictionary.load(BRAND_ALIASES_PATH),
        channel=channel,
    )


def build_collector(pipeline: AnalysisPipeline) -> DemandCollectionService:
    return DemandCollectionService(
        pipeline,
        client_factory=build_naver_client_factory(settings),
        shopping_client_factory=build_naver_client_factory(settings, shopping=True),
        anchor_keyword=settings.naver_anchor_keyword,
        category_codes=load_category_codes(NAVER_CATEGORIES_PATH),
    )


def build_searcher(pipeline: AnalysisPipeline) -> SupplierSearchService:
    return SupplierSearchService(
        pipeline, build_source_specs(settings),
        max_queries_per_product=settings.supplier_max_queries_per_product,
    )


def sync_channel(pipeline: AnalysisPipeline) -> None:
    """Re-score when the sales channel differs from the one stored candidates used."""
    opps = [o for o in pipeline.opportunities.list_latest() if not o.score_basis.get("not_evaluable")]
    stored = {((o.score_basis.get("decision") or {}).get("channel") or {}).get("key") for o in opps}
    if opps and stored != {pipeline.channel.key}:
        pipeline.reanalyze()


def show_rakuten_credit(pipeline: AnalysisPipeline) -> None:
    """Rakuten terms require the attribution snippet, unmodified, wherever its data is used."""
    src = pipeline.sources.get_by_name(RakutenIchibaSource.source_name)
    used = src is not None and any(o.source_id == src.id for o in pipeline.supplier_offers.list_all())
    if used or (settings.rakuten_application_id and settings.rakuten_access_key):
        st.markdown(RAKUTEN_CREDIT_HTML, unsafe_allow_html=True)  # fixed constant, not external input


def open_detail(product_id: int) -> None:
    """Callback: jump to the detail page for a product (runs before the next script run)."""
    st.session_state["detail_product"] = product_id
    st.session_state["page"] = "상품 상세"


# --------------------------------------------------------------------- job results


def remember(messages: list[tuple[str, str]], title: str) -> None:
    """Keep job results visible across page changes (shown at the top of every page)."""
    st.session_state["last_job"] = {"title": title, "at": datetime.now().strftime("%H:%M:%S"), "messages": messages}
    worst = "error" if any(m[0] == "error" for m in messages) else "warning" if any(
        m[0] == "warning" for m in messages) else "success"
    st.toast(f"{title} 완료" + (" — 확인할 내용이 있습니다" if worst != "success" else ""))


def render_last_job() -> None:
    job = st.session_state.get("last_job")
    if not job:
        return
    with st.expander(f"최근 작업 결과 · {job['title']} ({job['at']})", expanded=True):
        for level, text in job["messages"]:
            getattr(st, level)(text)
        if st.button("결과 닫기", key="close_job"):
            st.session_state.pop("last_job", None)
            st.rerun()


# --------------------------------------------------------------------- sidebar


def render_sidebar(pipeline, collector, searcher) -> str:
    with st.sidebar:
        st.radio("화면", PAGES, key="page")
        st.divider()
        costs = pipeline.costs
        keys = list(costs.channels)
        st.selectbox(
            "판매 채널 (수수료)", keys, key="channel",
            format_func=lambda k: f"{costs.channels[k].label} {costs.channels[k].fee_pct:.0%}",
            help="채널별 판매 수수료로 마진을 다시 계산합니다. 값은 config/scoring_config.yaml에서 수정합니다.",
        )
        st.caption(f"목표 마진 {settings.target_margin_pct:.0f}% · 비용 가정은 config/scoring_config.yaml")

        with st.expander("데이터 작업", expanded=not pipeline.products.list_all()):
            st.caption("① 가져오기 → ② 수요 수집 → ③ 공급처 탐색 → ④ 재분석 → ⑤ 내보내기")
            if st.button("샘플 CSV 가져오기 + 분석", type="primary", width="stretch"):
                s = pipeline.run_from_directory(SAMPLE_DIR, source_name=SAMPLE_SOURCE,
                                                source_notes="개발용 가상 샘플 데이터 — 실제 시장 데이터 아님")
                msgs = [("success",
                         f"신규 상품 {s.products_imported} · 해외오퍼 {s.supplier_offers_imported} · "
                         f"국내오퍼 {s.domestic_offers_imported} · 수요신호 {s.demand_signals_imported} · "
                         f"키워드 자동연결 {s.keyword_links_auto} / 검토필요 {s.keyword_links_review} · "
                         f"후보 {s.opportunities_created}건 산출")]
                msgs += [("warning", e) for e in s.errors[:5]]
                remember(msgs, "샘플 가져오기")

            naver_ok = settings.naver_configured or settings.naver_shopping_configured
            if st.button("네이버 수요 수집" + ("" if naver_ok else " (키 미설정)"), width="stretch"):
                with st.spinner("네이버 데이터랩 수집 중..."):
                    msgs = []
                    for o in collector.collect_all():
                        if o.status == RunStatus.SKIPPED:
                            msgs.append(("info", f"{o.source_name}: 건너뜀 — {o.message}"))
                            continue
                        level = "error" if o.status == RunStatus.FAILED else "success"
                        msgs.append((level, f"{o.source_name}: {RUN_LABELS[o.status.value]} · 신규 신호 "
                                            f"{o.signals_collected} · 호출 {o.calls_made}회"))
                        msgs += [("warning", e) for e in o.errors[:3]]
                        if o.message:
                            msgs.append(("info", f"{o.source_name}: {o.message}"))
                remember(msgs, "네이버 수요 수집")

            configured = [sp for sp in searcher.specs if sp.factory is not None]
            if st.button("해외 공급처 탐색" + ("" if configured else " (키 미설정)"), width="stretch"):
                with st.spinner("공급처 검색 중..."):
                    msgs = []
                    for o in searcher.search_all():
                        if o.status == RunStatus.SKIPPED:
                            msgs.append(("info", f"{o.source_name}: 건너뜀 — {o.message}"))
                            continue
                        level = "error" if o.status == RunStatus.FAILED else "success"
                        msgs.append((level, f"{o.source_name}: {RUN_LABELS[o.status.value]} · 검색 {o.queries}회 · "
                                            f"결과 {o.listings_seen} · 자동매칭 {o.matched_auto} · 검토필요 "
                                            f"{o.matched_review} · 신규 {o.new_observations} · 가격변동 {o.price_changes}"))
                        msgs += [("warning", e) for e in o.errors[:3]]
                remember(msgs, "공급처 탐색")
            if not naver_ok or not configured:
                st.caption("API 키 설정 방법은 '데이터 소스 관리' 화면과 README를 참고하세요. 키 없이도 CSV로 동작합니다.")

            if st.button("재분석", width="stretch"):
                remember([("success", f"{pipeline.reanalyze()}건 재산출")], "재분석")
            if st.button("CSV로 내보내기", width="stretch"):
                out = PROJECT_ROOT / "exports" / "opportunities.csv"
                remember([("success", f"{pipeline.export_opportunities_csv(out)}건 → {out}")], "CSV 내보내기")
    return st.session_state["page"]


# --------------------------------------------------------------------- dashboard


def render_dashboard(pipeline: AnalysisPipeline) -> None:
    products = pipeline.products.list_all()
    if not products:
        st.info("**시작하기** — 왼쪽 '데이터 작업'에서 **샘플 CSV 가져오기 + 분석**을 누르면 전체 흐름을 바로 볼 수 있습니다. "
                "내 데이터는 data/sample과 같은 형식의 CSV로 준비하세요.")
        return
    opps = pipeline.ranked_latest()
    tiers = [tier_of(o) for o in opps]
    c = st.columns(5)
    c[0].metric("소싱 검토", tiers.count(TIER_SOURCE), help=TIER_HELP[TIER_SOURCE])
    c[1].metric("조건부", tiers.count(TIER_CONDITIONAL), help=TIER_HELP[TIER_CONDITIONAL])
    c[2].metric("제외 권고", tiers.count(TIER_EXCLUDE), help=TIER_HELP[TIER_EXCLUDE])
    c[3].metric("검토 필요 키워드", len(pipeline.keywords.list_by_status(KeywordLinkStatus.NEEDS_REVIEW)))
    c[4].metric("등록 상품", len(products))

    top = [o for o in opps if tier_of(o) == TIER_SOURCE][:5]
    st.subheader("오늘 검토할 후보")
    if top:
        pmap = {p.id: p for p in products}
        for o in top:
            cols = st.columns([5, 2, 2, 1])
            cols[0].markdown(f"**{pmap[o.product_id].product_name}** — "
                             f"주의: {', '.join(flag_label(f) for f in sorted_flags(o.risk_flags)[:2]) or '없음'}")
            cols[1].write(f"마진 {o.estimated_margin_pct:.1f}%")
            cols[2].write(f"이익 {won(o.estimated_profit_krw)}")
            cols[3].button("상세", key=f"dash{o.product_id}", on_click=open_detail, args=(o.product_id,))
    else:
        st.caption("목표 마진을 넘는 후보가 없습니다. '소싱 후보 목록'에서 조건부 후보를 확인하세요.")

    signals = pipeline.demand.list_all()
    rising = sorted((s for s in signals if (s.growth_pct or 0) > 0), key=lambda s: -(s.growth_pct or 0))
    st.subheader("수요 상승 키워드")
    if rising:
        st.dataframe(
            pd.DataFrame([{"키워드": s.keyword, "성장률": s.growth_pct, "수요지수": s.demand_index,
                           "출처": pipeline._source_name(s.source_id),
                           "기간 종료": s.period_end.date() if s.period_end else None} for s in rising[:10]]),
            width="stretch", hide_index=True,
            column_config={"성장률": st.column_config.NumberColumn(format="%.1f%%")},
        )
    else:
        st.caption("성장률이 양수인 수요 신호가 없습니다.")

    st.subheader("경고")
    warnings = [f"데이터 소스 '{s.name}' 최근 오류: {s.last_error}" for s in pipeline.sources.list_all() if s.last_error]
    pmap = {p.id: p for p in products}
    for o in opps:
        sup = o.score_basis.get("supplier_offer", {})
        if sup and stale(sup.get("checked_at")):
            warnings.append(f"'{pmap[o.product_id].product_name}' 해외 가격 확인이 {STALE_PRICE_DAYS}일 이상 지남")
    for w in warnings[:15]:
        st.warning(w)
    if not warnings:
        st.caption("경고 없음")


# --------------------------------------------------------------------- demand


def render_demand(pipeline: AnalysisPipeline) -> None:
    st.subheader("수요 탐색")
    st.caption("수요 지수는 출처별 상대 지수(검색/클릭 관심도)이며 실제 판매량이 아닙니다.")
    signals = pipeline.demand.list_all()
    if not signals:
        st.info("수요 신호가 없습니다. '데이터 작업'에서 샘플 CSV를 가져오거나 네이버 수요 수집을 실행하세요.")
        return
    products = {p.id: p for p in pipeline.products.list_all()}

    pending = pipeline.keywords.list_by_status(KeywordLinkStatus.NEEDS_REVIEW)
    st.write(f"#### 검토 필요한 키워드 연결 ({len(pending)})")
    st.caption("확정한 연결만 수요 점수에 반영됩니다.")
    if not pending:
        st.caption("검토 대기 중인 연결이 없습니다.")
    for k in pending:
        p = products.get(k.product_id)
        cols = st.columns([5, 1, 1])
        cols[0].markdown(f"**{k.keyword}** → {p.product_name if p else k.product_id}  \n{k.reason}")
        if cols[1].button("연결 확정", key=f"c{k.id}", help=f"'{k.keyword}' 수요를 이 상품에 반영"):
            pipeline.keywords.set_status(k.id, KeywordLinkStatus.CONFIRMED)
            pipeline.reanalyze()
            st.rerun()
        if cols[2].button("연결 제외", key=f"r{k.id}", help="이 상품과 무관한 키워드로 표시"):
            pipeline.keywords.set_status(k.id, KeywordLinkStatus.REJECTED)
            pipeline.reanalyze()
            st.rerun()

    st.write("#### 수요 신호")
    source_names = sorted({pipeline._source_name(s.source_id) or "-" for s in signals})
    categories = sorted({s.category or "-" for s in signals})
    f1, f2, f3 = st.columns(3)
    q = f1.text_input("키워드 검색")
    sel_sources = f2.multiselect("데이터 소스", source_names, default=source_names)
    sel_cats = f3.multiselect("카테고리", categories, default=categories)
    link_map: dict[str, list[str]] = {}
    for k in pipeline.keywords.list_all():
        if k.status != KeywordLinkStatus.CANDIDATE:
            p = products.get(k.product_id)
            link_map.setdefault(k.normalized_keyword, []).append(
                f"{p.product_name if p else k.product_id} ({LINK_LABELS[k.status.value]})")
    rows = []
    for s in signals:
        src = pipeline._source_name(s.source_id) or "-"
        if src not in sel_sources or (s.category or "-") not in sel_cats:
            continue
        if q and q.lower() not in s.keyword.lower():
            continue
        rows.append({
            "키워드": s.keyword, "지표 유형": SIGNAL_LABELS.get(s.signal_type.value, s.signal_type.value),
            "수요지수": s.demand_index, "성장률": s.growth_pct,
            "신뢰도": CONFIDENCE_LABELS.get(s.confidence.value, s.confidence.value), "출처": src,
            "기간": f"{s.period_start.date() if s.period_start else '?'} ~ {s.period_end.date() if s.period_end else '?'}",
            "연결 상품": "; ".join(link_map.get(normalize_text(s.keyword), [])) or "연결 없음",
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                 column_config={"성장률": st.column_config.NumberColumn(format="%.1f%%")})


# --------------------------------------------------------------------- candidates


def render_opportunities(pipeline: AnalysisPipeline) -> None:
    st.subheader("소싱 후보 목록")
    opps = pipeline.ranked_latest()
    if not opps:
        st.info("후보가 없습니다. '데이터 작업'에서 가져오기 또는 재분석을 실행하세요.")
        return
    products = {p.id: p for p in pipeline.products.list_all()}
    f1, f2, f3 = st.columns(3)
    tier_sel = f1.multiselect("판정", list(TIER_LABELS), default=[TIER_SOURCE, TIER_CONDITIONAL, TIER_EXCLUDE],
                              format_func=TIER_LABELS.get)
    statuses = f2.multiselect("검토 상태", [s.value for s in ReviewStatus],
                              default=[s.value for s in ReviewStatus], format_func=REVIEW_LABELS.get)
    min_score = f3.slider("최소 종합점수", 0, 100, 0)

    rows, ids = [], []
    for o in opps:
        tier = tier_of(o)
        if tier not in tier_sel or o.review_status.value not in statuses or o.total_score < min_score:
            continue
        p = products.get(o.product_id)
        dec = o.score_basis.get("decision") or {}
        top = sorted_flags(o.risk_flags)
        ids.append(o.product_id)
        rows.append({
            "판정": TIER_LABELS[tier],
            "상품": p.product_name if p else o.product_id,
            "모델": (p.model_number or "") if p else "",
            "예상 마진": o.estimated_margin_pct,
            "예상 이익": o.estimated_profit_krw,
            "목표 마진 판매가": dec.get("required_sale_price_krw"),
            "시장 최저가": dec.get("market_lowest_krw"),
            "종합점수": o.total_score,
            "주요 위험": ", ".join(flag_label(f) for f in top[:2]) + (f" 외 {len(top) - 2}건" if len(top) > 2 else ""),
            "신뢰도": CONFIDENCE_LABELS.get(o.confidence.value, o.confidence.value),
            "검증": "검증 완료" if is_verified(o) else "미검증",
            "검토상태": REVIEW_LABELS[o.review_status.value],
        })
    if not rows:
        st.caption("조건에 맞는 후보가 없습니다.")
        return
    event = st.dataframe(
        pd.DataFrame(rows), width="stretch", hide_index=True,
        on_select="rerun", selection_mode="single-row", key="opp_table",
        column_config={
            "예상 마진": st.column_config.NumberColumn(format="%.1f%%"),
            "예상 이익": st.column_config.NumberColumn(format="%,d원"),
            "목표 마진 판매가": st.column_config.NumberColumn(
                format="%,d원", help=f"목표 마진 {settings.target_margin_pct:.0f}%를 내려면 필요한 판매가"),
            "시장 최저가": st.column_config.NumberColumn(format="%,d원"),
            "종합점수": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f"),
        },
    )
    selected = event.selection.rows if event and event.selection else []
    default_idx = selected[0] if selected else 0
    c1, c2 = st.columns([4, 1])
    pid = c1.selectbox("상세로 열 상품 (표에서 행을 선택해도 됩니다)", ids, index=default_idx,
                       format_func=lambda i: products[i].product_name, key="opp_pick")
    c2.button("상세 보기 →", on_click=open_detail, args=(pid,), type="primary", width="stretch")
    st.caption("정렬: 판정(소싱 검토 → 조건부 → 제외 권고) 후 종합점수. 원가·관부가세·국제배송비는 추정치를 포함합니다.")


# --------------------------------------------------------------------- detail


def render_detail(pipeline: AnalysisPipeline) -> None:
    products = pipeline.products.list_all()
    if not products:
        st.info("상품이 없습니다. '데이터 작업'에서 가져오기를 먼저 실행하세요.")
        return
    pmap = {p.id: p for p in products}
    if st.session_state.get("detail_product") not in pmap:
        st.session_state["detail_product"] = products[0].id
    pid = st.selectbox("상품", list(pmap), key="detail_product",
                       format_func=lambda i: f"{pmap[i].product_name} ({pmap[i].model_number or '모델 없음'})")
    product = pmap[pid]
    opp = pipeline.opportunities.latest_for_product(product.id)
    if not opp:
        st.info("이 상품의 후보가 아직 산출되지 않았습니다 (해외·국내 오퍼 필요).")
        render_offer_decisions(pipeline, product)
        return

    b = opp.score_basis
    dec = b.get("decision") or {}
    tier = tier_of(opp)
    banner = {TIER_SOURCE: st.success, TIER_CONDITIONAL: st.warning, TIER_EXCLUDE: st.error}[tier]
    banner(f"**{TIER_LABELS[tier]}** — {TIER_HELP[tier]}")
    if b.get("not_evaluable"):
        st.info(b.get("note", ""))
    c = st.columns(4)
    c[0].metric("예상 마진", f"{opp.estimated_margin_pct:.1f}%" if opp.estimated_margin_pct is not None else "-")
    c[1].metric("예상 이익", won(opp.estimated_profit_krw))
    req, mkt = dec.get("required_sale_price_krw"), dec.get("market_lowest_krw")
    c[2].metric(f"목표 마진({settings.target_margin_pct:.0f}%) 판매가", won(req),
                delta=(f"시장 최저가 대비 {req - mkt:+,.0f}원" if req and mkt else None), delta_color="inverse")
    c[3].metric("판매가 기준", won(dec.get("sale_price_krw")),
                help="내 판매가를 입력하지 않으면 국내 시장 최저가로 판다고 가정합니다.")
    if dec.get("sale_price_source") == "domestic_lowest":
        st.caption(f"판매가 = 국내 시장 최저가 · 채널 {(dec.get('channel') or {}).get('label', '-')} · "
                   "아래 '판매 조건'에서 내 판매가를 입력할 수 있습니다.")
    for r in b.get("regulations", []):
        st.warning(f"수입·배송 확인: {r['label']}")
    other = [f for f in sorted_flags(opp.risk_flags) if not f.startswith("reg:")]
    if other:
        st.caption("위험 요소: " + ", ".join(flag_label(f) for f in other))
    if not b.get("not_evaluable") and stale(b.get("supplier_offer", {}).get("checked_at")):
        st.warning(f"해외 가격 확인이 {STALE_PRICE_DAYS}일 이상 지났습니다. 재확인이 필요합니다.")

    tabs = st.tabs(["수익 계산", "공급처·국내가", "점수 근거", "수요·키워드", "검토·이력"])
    with tabs[0]:
        render_cost_tab(pipeline, product, opp)
    with tabs[1]:
        render_offer_decisions(pipeline, product)
    with tabs[2]:
        render_score_tab(opp)
    with tabs[3]:
        render_keywords_tab(pipeline, product, opp)
    with tabs[4]:
        render_review_tab(pipeline, product, opp)


def render_cost_tab(pipeline, product, opp) -> None:
    b = opp.score_basis
    cost = b.get("cost") or {}
    if cost.get("breakdown"):
        bd = cost["breakdown"]
        rows = [{"항목": label, "금액": bd.get(k, 0.0)} for k, label in COST_LABELS.items() if k in bd]
        rows.append({"항목": "총비용", "금액": cost.get("total_cost_krw")})
        rows.append({"항목": "판매가", "금액": bd.get("domestic_sale_price_krw")})
        rows.append({"항목": "예상 이익", "금액": opp.estimated_profit_krw})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     column_config={"금액": st.column_config.NumberColumn(format="%,d원")})
        assumptions = cost.get("assumptions") or {}
        if assumptions:
            st.write("**적용한 가정** (추정치는 실제 견적·통관 기준으로 확인하세요)")
            st.dataframe(pd.DataFrame([{"항목": k, "내용": v} for k, v in assumptions.items()]),
                         hide_index=True, width="stretch")
        fx = b.get("fx") or {}
        if fx:
            st.caption(f"환율: 1 {fx.get('currency')} = {fx.get('rate_to_krw'):,} 원 · 제공 {fx.get('provider')} · "
                       f"기준 {(fx.get('fetched_at') or '-')[:16]}")
    else:
        st.caption("비용을 계산할 수 없습니다 (가격 비교에 쓸 오퍼 없음).")

    st.write("**판매 조건**")
    with st.form(f"sale{product.id}"):
        c1, c2 = st.columns(2)
        my_price = c1.number_input("내 판매가 (원, 0 = 시장 최저가 사용)", min_value=0, step=1000,
                                   value=int(product.my_sale_price_krw or 0))
        weight = c2.number_input("배송 무게 (kg, 0 = 카테고리 기본값)", min_value=0.0, step=0.1,
                                 value=float(product.weight_kg or 0.0))
        if st.form_submit_button("저장 후 재계산"):
            pipeline.products.update_fields(product.id, my_sale_price_krw=my_price or None,
                                            weight_kg=weight or None)
            pipeline.reanalyze()
            st.rerun()


def render_score_tab(opp) -> None:
    b = opp.score_basis
    w = b.get("weights", {})
    inputs = b.get("inputs", {})
    rows = [
        ("수요", opp.demand_score, "demand", inputs.get("demand_index")),
        ("성장률", opp.growth_score, "growth", inputs.get("growth_pct")),
        ("가격 경쟁력", opp.price_score, "price", inputs.get("price_gap_ratio")),
        ("마진", opp.margin_score, "margin", inputs.get("margin_pct")),
        ("경쟁(추정)", opp.competition_score, "competition", inputs.get("competitor_count")),
    ]
    df = pd.DataFrame([{"항목": n, "점수": s, "가중치": w.get(k), "기여": round(s * (w.get(k) or 0), 1), "입력값": v}
                       for n, s, k, v in rows])
    st.metric("종합점수", opp.total_score)
    st.dataframe(df, hide_index=True, width="stretch",
                 column_config={"점수": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f"),
                                "가중치": st.column_config.NumberColumn(format="%.2f")})
    st.caption("종합점수는 순위 참고용입니다. 소싱 판단은 위의 판정(마진·필요 판매가)을 우선하세요. " + b.get("note", ""))


def render_keywords_tab(pipeline, product, opp) -> None:
    demand = opp.score_basis.get("demand", {})
    if demand.get("signals"):
        st.write("**점수에 반영된 수요 신호**")
        st.dataframe(pd.DataFrame(demand["signals"]), width="stretch", hide_index=True)
    else:
        st.caption("점수에 반영된 수요 신호가 없습니다.")
    kws = pipeline.keywords.list_by_product(product.id)
    active = [k for k in kws if k.status != KeywordLinkStatus.CANDIDATE]
    st.write("**연결된 키워드**")
    if not active:
        st.caption("연결된 수요 키워드가 없습니다.")
    for k in active:
        cols = st.columns([3, 1, 3, 1, 1])
        cols[0].write(k.keyword)
        cols[1].write(LINK_LABELS[k.status.value])
        cols[2].caption(k.reason or "")
        if k.status != KeywordLinkStatus.CONFIRMED:
            if cols[3].button("연결 확정", key=f"dc{k.id}"):
                pipeline.keywords.set_status(k.id, KeywordLinkStatus.CONFIRMED)
                pipeline.reanalyze()
                st.rerun()
        if k.status != KeywordLinkStatus.REJECTED:
            if cols[4].button("연결 제외", key=f"dr{k.id}"):
                pipeline.keywords.set_status(k.id, KeywordLinkStatus.REJECTED)
                pipeline.reanalyze()
                st.rerun()
    cands = [k for k in kws if k.status == KeywordLinkStatus.CANDIDATE]
    with st.expander(f"수요 수집용 검색어 후보 {len(cands)}개"):
        st.caption("상품 정보와 사전(brand_aliases.yaml)으로 만든 검색어입니다. 수요 수집 때 사용됩니다.")
        st.dataframe(pd.DataFrame([{"검색어": k.keyword, "생성 근거": k.reason} for k in cands]),
                     hide_index=True, width="stretch")


def render_review_tab(pipeline, product, opp) -> None:
    keys = [s.value for s in ReviewStatus]
    new_status = st.selectbox("검토 상태", keys, index=keys.index(opp.review_status.value),
                              format_func=REVIEW_LABELS.get, key=f"rs{opp.id}")
    if new_status != opp.review_status.value and st.button("상태 저장"):
        pipeline.opportunities.update_review_status(opp.id, ReviewStatus(new_status))
        st.rerun()
    if new_status == ReviewStatus.APPROVED.value and opp.risk_flags:
        st.info("위험 요소가 남아 있어 '소싱 확정' 후에도 '검증 완료'로 표시되지 않습니다.")
    history = pipeline.opportunities.history_for_product(product.id)
    if len(history) > 1:
        st.write("**점수·마진 이력**")
        st.line_chart(pd.DataFrame({"종합점수": [h.total_score for h in history],
                                    "마진(%)": [h.estimated_margin_pct for h in history]}))
    with st.expander("해외 가격 관측 이력 (전체)"):
        st.dataframe(pd.DataFrame([{
            "공급처": o.supplier_name, "가격": o.foreign_price, "통화": o.currency,
            "배송비": o.shipping_cost if o.shipping_cost_known else None,
            "상태": VERIFY_LABELS[o.verification_status.value],
            "확인 시각": o.price_checked_at, "URL": o.product_url,
        } for o in pipeline.supplier_offers.list_by_product(product.id)]), hide_index=True, width="stretch",
            column_config={"URL": st.column_config.LinkColumn()})
    with st.expander("국내 가격 관측 이력 (전체)"):
        st.dataframe(pd.DataFrame([{
            "판매처": d.seller_name, "가격": d.price_krw, "배송비": d.shipping_cost_krw if d.shipping_cost_known else None,
            "상태": VERIFY_LABELS[d.verification_status.value], "확인 시각": d.price_checked_at, "URL": d.product_url,
        } for d in pipeline.domestic_offers.list_by_product(product.id)]), hide_index=True, width="stretch",
            column_config={"URL": st.column_config.LinkColumn()})


def _decision_buttons(cols, key: str, status: VerificationStatus, on_set, what: str) -> None:
    if status != VerificationStatus.VERIFIED and cols[0].button(
            f"{what} 확정", key=f"v{key}", help="동일 상품이 맞음 — 가격 비교에 확정 반영"):
        on_set(VerificationStatus.VERIFIED)
    if status != VerificationStatus.REJECTED and cols[1].button(
            f"{what} 제외", key=f"x{key}", help="다른 상품이거나 쓰지 않을 오퍼 — 다시 검색해도 제외 유지"):
        on_set(VerificationStatus.REJECTED)


def render_offer_decisions(pipeline: AnalysisPipeline, product) -> None:
    st.write("**해외 공급처 후보** (최신 관측 기준)")
    st.caption("최저가만으로 추천하지 않습니다. '검토 필요'·'제외'·신뢰도 미달 오퍼는 가격 비교에서 빠지고, "
               "자동 매칭은 가격에 반영되지만 확정 전까지 '미검증'으로 표시됩니다.")
    cands = pipeline.supplier_candidates(product.id)
    if not cands:
        st.caption("해외 공급처 오퍼가 없습니다. CSV로 가져오거나 '해외 공급처 탐색'을 실행하세요.")
    for c in cands:
        o, t = c["offer"], c["trust"]
        cols = st.columns([5, 2, 1, 1])
        landed = won(c["landed_krw"]) if c["landed_krw"] is not None else "환산 불가"
        state = "✅ 가격 반영" if c["eligible"] else f"⛔ 제외됨 — {c['excluded_reason']}"
        link = f"[{o.supplier_name}]({o.product_url})" if o.product_url else o.supplier_name
        ship = f"{o.shipping_cost:,.2f}" if o.shipping_cost_known else "미확인"
        lead = f" · 배송 약 {o.lead_time_days}일" if o.lead_time_days else ""
        cols[0].markdown(f"**{link}** · {o.foreign_price:,.2f} {o.currency} (+판매처 배송 {ship}) ≈ {landed}{lead}  \n"
                         f"{state}  \n{o.listing_title or ''}  \n근거: {o.match_reason or '-'}")
        cols[1].markdown(f"{VERIFY_LABELS[o.verification_status.value]}  \n신뢰도 {t.score:.0f}점  \n"
                         + ", ".join(t.flags[:2]))

        def _set(status, oid=o.id):
            pipeline.supplier_offers.set_status_for_listing(oid, status)
            pipeline.reanalyze()
            st.rerun()

        _decision_buttons(cols[2:], f"s{o.id}", o.verification_status, _set, "공급처")

    st.write("**국내 비교 상품**")
    latest = pipeline._latest_observations(
        pipeline.domestic_offers.list_by_product(product.id), lambda d: (d.seller_name, d.product_url))
    if not latest:
        st.caption("국내 비교 상품이 없습니다.")
    for d in sorted(latest, key=lambda d: d.total_price_krw):
        cols = st.columns([5, 2, 1, 1])
        link = f"[{d.seller_name}]({d.product_url})" if d.product_url else d.seller_name
        ship = won(d.shipping_cost_krw) if d.shipping_cost_known else "배송비 미확인"
        cols[0].markdown(f"**{link}** · {d.price_krw:,.0f}원 (배송 {ship})  \n{d.listing_title or ''}  \n"
                         f"근거: {d.match_reason or '-'}")
        cols[1].write(VERIFY_LABELS[d.verification_status.value])

        def _setd(status, did=d.id):
            pipeline.domestic_offers.set_status_for_listing(did, status)
            pipeline.reanalyze()
            st.rerun()

        _decision_buttons(cols[2:], f"d{d.id}", d.verification_status, _setd, "국내가")


# --------------------------------------------------------------------- suppliers


def render_suppliers(pipeline: AnalysisPipeline) -> None:
    st.subheader("해외 공급처")
    products = {p.id: p for p in pipeline.products.list_all()}
    if not products:
        st.info("상품이 없습니다.")
        return
    rows, ids = [], []
    for pid, p in products.items():
        cands = pipeline.supplier_candidates(pid)
        eligible = [c for c in cands if c["eligible"]]
        best = eligible[0] if eligible else None
        ids.append(pid)
        rows.append({
            "상품": p.product_name, "모델": p.model_number,
            "후보 수": len(cands), "가격 반영 가능": len(eligible),
            "검토 필요": sum(c["offer"].verification_status == VerificationStatus.NEEDS_REVIEW for c in cands),
            "최저 적격 공급처": best["offer"].supplier_name if best else "-",
            "최저 적격가": round(best["landed_krw"]) if best and best["landed_krw"] else None,
            "상태": VERIFY_LABELS[best["offer"].verification_status.value] if best else "-",
            "신뢰도": best["trust"].score if best else None,
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True,
                 column_config={"최저 적격가": st.column_config.NumberColumn(format="%,d원")})
    c1, c2 = st.columns([4, 1])
    pid = c1.selectbox("오퍼를 확정/제외할 상품", ids, format_func=lambda i: products[i].product_name, key="sup_pick")
    c2.button("상세에서 확정/제외 →", on_click=open_detail, args=(pid,), width="stretch")

    with st.expander("공급처 속성 관리 (신뢰 수준·반품·구매 제한)"):
        suppliers = pipeline.suppliers.list_all()
        if not suppliers:
            st.caption("등록된 공급처가 없습니다.")
            return
        names = [s.name for s in suppliers]
        sup = suppliers[names.index(st.selectbox("공급처", names))]
        with st.form(f"sup{sup.id}"):
            a, b_, c = st.columns(3)
            trust = a.selectbox("신뢰 수준", [t.value for t in TrustLevel], format_func=TRUST_LABELS.get,
                                index=[t.value for t in TrustLevel].index(sup.trust_level.value))
            stype = b_.selectbox("공급처 유형", [t.value for t in SupplierType], format_func=SUPPLIER_TYPE_LABELS.get,
                                 index=[t.value for t in SupplierType].index(sup.supplier_type.value))
            ships_opts = ["미확인", "가능", "불가"]
            ships = c.selectbox("한국 배송", ships_opts,
                                index=0 if sup.ships_to_kr is None else (1 if sup.ships_to_kr else 2))
            country = st.text_input("국가", sup.country or "")
            returns = st.text_input("반품 정책", sup.return_policy or "")
            restrict = st.text_input("구매 제한 / 이용 조건", sup.purchase_restrictions or "")
            auth = st.text_input("정품·지식재산권 메모", sup.authenticity_notes or "")
            notes = st.text_area("메모", sup.notes or "")
            if st.form_submit_button("저장"):
                pipeline.suppliers.update(sup.model_copy(update={
                    "trust_level": TrustLevel(trust), "supplier_type": SupplierType(stype),
                    "ships_to_kr": None if ships == "미확인" else ships == "가능",
                    "country": country or None, "return_policy": returns or None,
                    "purchase_restrictions": restrict or None, "authenticity_notes": auth or None,
                    "notes": notes or None,
                }))
                pipeline.reanalyze()
                st.success("저장 후 재분석했습니다.")


# --------------------------------------------------------------------- sources & runs


def render_sources(pipeline: AnalysisPipeline) -> None:
    st.subheader("데이터 소스 관리")
    ebay = bool(settings.ebay_client_id and settings.ebay_client_secret)
    rakuten = bool(settings.rakuten_application_id and settings.rakuten_access_key)
    st.dataframe(pd.DataFrame([
        {"API": "NAVER API HUB — 검색어 트렌드", "인증": "설정됨" if settings.naver_configured else "미설정",
         "환경변수": "NAVER_CLIENT_ID / NAVER_CLIENT_SECRET"},
        {"API": "NAVER API HUB — 쇼핑 인사이트",
         "인증": ("설정됨(전용 키)" if settings.naver_shopping_has_own_keys
                  else "설정됨(공용 키)" if settings.naver_shopping_configured else "미설정"),
         "환경변수": "NAVER_SHOPPING_CLIENT_ID / NAVER_SHOPPING_CLIENT_SECRET (비우면 위 키 사용)"},
        {"API": "eBay Browse", "인증": "설정됨" if ebay else "미설정", "환경변수": "EBAY_CLIENT_ID / EBAY_CLIENT_SECRET"},
        {"API": "라쿠텐 이치바", "인증": "설정됨" if rakuten else "미설정",
         "환경변수": "RAKUTEN_APPLICATION_ID / RAKUTEN_ACCESS_KEY"},
    ]), hide_index=True, width="stretch")
    st.caption("키는 프로젝트 폴더의 .env 파일에 넣고 앱을 다시 시작합니다 (copy .env.example .env). 발급 방법은 README 참고. "
               f"네이버 실행당 호출 상한 {settings.naver_max_calls_per_run}회 · 기준 키워드 '{settings.naver_anchor_keyword}' · "
               f"쇼핑 분야 코드 {len(load_category_codes(NAVER_CATEGORIES_PATH))}개")
    for src in pipeline.sources.list_all():
        state = "🟢 활성" if src.enabled else "⚪ 비활성"
        err = " · 최근 오류 있음" if src.last_error else ""
        with st.expander(f"{state} · {src.name}{err}"):
            st.write(f"유형: {src.source_type} / {src.access_method}")
            st.write(f"마지막 성공: {str(src.last_success_at)[:19] if src.last_success_at else '-'}")
            st.write(f"최근 오류: {src.last_error or '-'}")
            if src.terms_url:
                st.write(f"이용 조건/문서: {src.terms_url}")
            st.caption(src.license_notes or "")
            enabled = st.toggle("이 소스 사용", value=src.enabled, key=f"src{src.id}")
            if enabled != src.enabled:
                pipeline.sources.set_enabled(src.id, enabled)
                st.rerun()


def _runs_table(pipeline: AnalysisPipeline, limit: int | None = None) -> None:
    runs = pipeline.runs.list_all()
    if not runs:
        st.info("작업 기록이 없습니다.")
        return
    st.dataframe(pd.DataFrame([
        {"소스": pipeline._source_name(r.source_id), "상태": RUN_LABELS.get(r.status.value, r.status.value),
         "수집": r.records_collected, "실패": r.records_failed,
         "시작": str(r.started_at)[:19], "종료": str(r.finished_at)[:19] if r.finished_at else "",
         "오류/사유": r.error_summary or ""}
        for r in runs[:limit]]), width="stretch", hide_index=True)


def render_runs(pipeline: AnalysisPipeline) -> None:
    st.subheader("작업 및 로그")
    _runs_table(pipeline)
    st.caption("실패한 수집은 '데이터 작업'의 같은 버튼으로 다시 실행할 수 있습니다.")


# --------------------------------------------------------------------- main


def main() -> None:
    st.set_page_config(page_title="Overseas Demand Radar", layout="wide")
    st.title("Overseas Demand Radar")
    st.caption("한국 수요 신호 기반 해외 구매대행 상품 발굴")

    db = get_database()
    st.session_state.setdefault("page", PAGES[0])
    probe = build_pipeline(db, None)
    st.session_state.setdefault("channel", probe.costs.default_channel)
    pipeline = build_pipeline(db, st.session_state["channel"])
    page = render_sidebar(pipeline, build_collector(pipeline), build_searcher(pipeline))
    pipeline.set_channel(st.session_state["channel"])
    sync_channel(pipeline)

    sample = pipeline.sources.get_by_name(SAMPLE_SOURCE)
    if sample is not None and pipeline.products.list_all():
        st.warning("현재 데이터에 **개발용 가상 샘플**이 포함되어 있습니다. 가격·수요 수치는 실제 시장 정보가 아닙니다.")
    render_last_job()
    {
        "대시보드": render_dashboard,
        "수요 탐색": render_demand,
        "소싱 후보 목록": render_opportunities,
        "상품 상세": render_detail,
        "해외 공급처": render_suppliers,
        "데이터 소스 관리": render_sources,
        "작업 및 로그": render_runs,
    }[page](pipeline)
    show_rakuten_credit(pipeline)


main()
