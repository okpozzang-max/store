# 다음 세션 인계 (2026-10-17 기준)

## 현재 상태
- Phase 1~3 완료 + 전문가 리뷰(구매대행 업자·UI/UX) 반영 완료. 상세 기능은 README.md 참고.
- 테스트: `.venv\Scripts\python.exe -m pytest` → **109 passed**
- 실행: `.venv\Scripts\python.exe -m streamlit run app.py`
- 한글 콘솔 출력 시 `$env:PYTHONIOENCODING="utf-8"` 필요. pytest tmp는 `pytest.ini`의 `--basetemp=.pytest_tmp` 사용 (사용자명 공백 경로 문제 회피).

## API 연동 상태
| API | .env 키 | 실호출 검증 |
|---|---|---|
| NAVER API HUB 검색어 트렌드 | `NAVER_CLIENT_ID/SECRET` | ✅ HTTP 200, 앱 버튼으로 수집·재분석까지 확인 |
| NAVER API HUB 쇼핑 인사이트 | `NAVER_SHOPPING_CLIENT_ID/SECRET` (전용 키) | ✅ HTTP 200. 단 앱에서는 분야 코드 미설정으로 건너뜀 |
| eBay Browse | 없음 | ❌ 미검증 (가짜 transport 테스트만) |
| 라쿠텐 이치바 | 없음 | ❌ 미검증 |
| 네이버 쇼핑 검색(국내가) | — | 2026-07-31 종료, 국내가는 `domestic_offers.csv`로만 입력 |

- 엔드포인트: `https://naverapihub.apigw.ntruss.com` + `/search-trend/v1/search`, `/shopping/v1/category/keywords`
- 헤더: `X-NCP-APIGW-API-KEY-ID` / `X-NCP-APIGW-API-KEY` (NCP 공식 문서와 일치 확인)

## 마지막 세션에서 고친 것 (실호출 결과 기반)
- 수요지수: `50 + 25·log10(키워드/기준키워드)`로 변경 (선형식은 실데이터에서 전부 100으로 포화)
- 검색량 없는 키워드는 오류가 아닌 "데이터 없음"으로 분리 (`BatchResult.no_data`, `CollectionOutcome.no_data_keywords`)

## 바로 할 일 (사용자 입력 필요)
1. `config/naver_categories.yaml`에 쇼핑 분야 코드 입력. 실측: `50000003`은 "다이슨 에어랩" 데이터 있음(분야명 미확인), `50000002`는 없음. 네이버쇼핑 URL의 `cat_id`로 확인.
2. `config/brand_aliases.yaml`에 한글 상품명/별칭 보강 — 모델번호·영문명은 국내 검색량이 거의 없음.
3. eBay(Production 키셋)·라쿠텐(App ID + Access Key) 키 발급 후 `.env` 입력 → 같은 방식으로 실호출 검증.
4. `config/scoring_config.yaml`의 배송대행 요율·관세율·채널 수수료를 실제 값으로 교체 (현재 일반 추정치).

## 다음 개발 후보
- Phase 4: 정기 수집(스케줄러), 가격 변동/목표 마진 충족 알림, 실패 작업 재실행, 판매 결과 기반 점수 보정
- 업자 리뷰 P2 미반영: 통관고유부호·구매 한도 관리
- 환율 자동 수집 (현재 `data/sample/exchange_rates.csv` 수동)

## 실호출 검증 방법 (재사용)
임시 폴더에 스크립트를 두고 `Settings()`로 키를 읽어 `NaverDataLabClient(..., max_calls=1)`로 1회 호출,
결과는 파일로 저장해 확인. 비밀값은 출력·DB에 남지 않는지 함께 검사하고 끝나면 임시 폴더 삭제.
앱 단위는 `DATABASE_PATH`를 임시 DB로 지정하고 `streamlit.testing.v1.AppTest`로 사이드바 버튼 클릭.

## 주요 설정 파일
- `.env` (Git 제외) / `.env.example`
- `config/scoring_config.yaml` — 가중치, 판매 채널, 원가 가정, 면세 기준
- `config/import_rules.yaml` — 수입·배송 규제 경고
- `config/brand_aliases.yaml` — 한글/현지어 사전
- `config/naver_categories.yaml` — 쇼핑 인사이트 분야 코드
- `data/overseas_demand_radar.db` — 실사용 DB (샘플 가져오기 이력 포함)
