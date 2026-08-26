# CLAUDE.md — my-geo-audit 프로젝트 가이드

## 한눈에 보기

LG.com 콘텐츠의 **AI Readability / GEO(Generative Engine Optimization) 점수**를 산정하는 Audit 도구.

- **백엔드**: FastAPI (`main.py`) + httpx · BeautifulSoup4 · Playwright (CSR)
- **점수 산정**: 룰 엔진(`rule_engine.py`) + 설정 파일(`scoring_config.json`)
- **프론트**: Tailwind 단일 페이지 (`static/index.html`), 어드민 (`static/admin.html`)
- **배포**: Render (`render.yaml`, `build.sh`)

## 디렉토리 구조

```
my-geo-audit/
├── main.py                       # FastAPI 앱 (라우팅, 보안, Rate Limit, 어드민)
├── analyzer.py                   # URL 분석 핵심 (페이지 fetch, JSON-LD, CSR, 점수 계산)
├── rule_engine.py                # 룰 엔진 — 34개 룰 타입 + 핸들러
├── csr_local.py                  # 로컬 SSR/CSR CLI
├── scoring_config.json           # 채점 설정 (어드민에서 수정)
├── scoring_config.default.json   # 팩토리 기본값 스냅샷 (리셋용)
├── audit_data.json               # 어드민 그룹/스케줄 (자동 생성)
├── docs/
│   └── audit-criteria.md         # 48개 항목 PASS/FAIL 기준 (source of truth)
├── tests/
│   └── test_rule_engine.py       # 57개 단위 테스트
├── static/
│   ├── index.html                # 분석 UI (4개 카테고리 카드)
│   └── admin.html                # 어드민 (기준/그룹/스케줄/문서)
└── extension/                    # Chrome 확장 (MV3)
```

## 카테고리와 항목 (총 49 / 100점)

| 카테고리 (key) | max | 항목 | 활성 |
| :-- | :-: | :-: | :-: |
| `performance`   | 25 | 11 | 7 (LCP/CLS/INP는 PSI API 필요로 비활성, #8 Render Blocking은 기준 제외) |
| `accessibility` | 15 | 4  | 4 |
| `seo`           | 20 | 8  | 8 (#17은 meta robots + X-Robots-Tag로 분리) |
| `ai_readiness`  | 40 | 26 | 26 |

기준 원본: [docs/audit-criteria.md](docs/audit-criteria.md) — 어드민 "Audit 기준 문서" 메뉴에서도 조회 가능.

## 룰 엔진 데이터 흐름

```
URL 입력
   │
   ▼
analyzer._fetch_page(url)
   ├─ httpx 요청 + 타이밍 측정
   ├─ → page_data: {soup, http_status, headers, http_version,
   │                 html_bytes, ttfb_ms, redirect_count, raw_html, final_url}
   ▼
analyzer._extract_json_ld(page_data)
   └─ → jsonld: {schemas, all_types, raw}
   ▼
analyze_url() 컨텍스트 빌드
   ├─ context = {soup, page_data, jsonld_types, jsonld_raw,
   │             base_url, current_url, csr_ratio_dict}
   ▼
analyzer._calculate_score(context, robots, csr_ratio)
   ├─ scoring_config.json의 활성 카테고리/항목 순회
   ├─ 항목별 rule_engine.evaluate_rule_async(rule, context) 호출
   ├─ 룰 타입에 맞는 핸들러(_HANDLERS / _ASYNC_HANDLERS) 실행
   └─ → score: {total, max, grade, breakdown: {<cat>: {points, max, passed, total, items: {<id>: {pass, value, hint}}}}}
   ▼
응답: data.score.breakdown.{performance|accessibility|seo|ai_readiness}.items
```

프론트(`renderCategoryCards`)는 `data.score.breakdown`을 직접 순회해 4개 카드를 렌더한다.

## 새 룰 타입 추가 절차

룰 타입은 1) 메타데이터 정의 2) 핸들러 함수 3) 레지스트리 등록 — 3 단계로 추가한다.

### 1. `rule_engine.py::RULE_TYPES`에 메타데이터 등록

```python
RULE_TYPES["my_new_rule"] = {
    "label": "내 룰 이름",
    "description": "설명",
    "params": {
        "threshold": {"label": "임계값", "type": "number", "placeholder": "10"},
    },
}
```

`params` 의 `type`은 `text` / `number` / `select`(`options` 배열 동반)을 지원. 어드민 UI가 자동으로 입력 폼을 그려준다.

### 2. 핸들러 함수 작성

```python
def _eval_my_new_rule(params: dict, ctx: dict) -> dict:
    threshold = int(params.get("threshold", 10))
    soup = ctx.get("soup")
    # ctx에서 필요한 데이터 사용:
    # - ctx["soup"]                  : BeautifulSoup
    # - ctx["page_data"]             : {http_status, headers, http_version, html_bytes, ttfb_ms, ...}
    # - ctx["jsonld_raw"]            : 원본 JSON-LD list[dict]
    # - ctx["jsonld_types"]          : 소문자 @type set
    # - ctx["base_url"] / current_url
    # - ctx["csr_ratio_dict"]        : {status, ratio, ...}
    ...
    return {
        "pass":  True_or_False,
        "value": "표시할 값",         # nullable
        "hint":  "실패 사유",          # PASS 시 None
    }
```

비동기가 필요하면 (HTTP fetch 등) `async def` 로 작성하고 `_ASYNC_HANDLERS`에 등록.

### 3. 레지스트리 등록

```python
_HANDLERS["my_new_rule"] = _eval_my_new_rule
# 또는 async일 경우
_ASYNC_HANDLERS["my_new_rule"] = _eval_my_new_rule
```

### 4. (선택) 테스트 추가

`tests/test_rule_engine.py` 적절한 클래스에 PASS/FAIL 케이스 추가.

## 새 항목/카테고리 추가 절차

### 카테고리 추가

1. `scoring_config.json` 최상위에 새 카테고리 키 추가:
```json
"my_category": {
  "max": 10,
  "label": "표시명",
  "description": "설명",
  "criteria": [...]
}
```
2. `scoring_config.default.json`도 동일하게 업데이트 (리셋 대비).
3. `analyzer.py::_calculate_score`의 `new_cat_keys` 리스트에 추가.
4. `static/index.html`의 `CATEGORY_META`에 라벨/색상/아이콘 추가.
5. `static/index.html`의 `renderCategoryCards`의 `order` 배열에 추가.
6. (선택) bulk 테이블 컬럼 추가.

### 항목 추가 (기존 카테고리 내)

`scoring_config.json`의 해당 카테고리 `criteria` 배열에 항목 추가:

```json
{
  "id":       "ai_my_check",            // prefix로 카테고리 표시
  "name":     "내 점검 항목",
  "spec_id":  "#46",                    // docs와 매칭
  "points":   2,
  "enabled":  true,
  "rule": {
    "type":   "my_new_rule",
    "params": { "threshold": 10 }
  }
}
```

`points` 합이 카테고리 `max`를 초과하면 **자동 클리핑**된다 (`_calculate_score` 내 `min(cat_score, cat_max)`).

## 컨텍스트 확장 절차

새 룰이 `page_data` 외 다른 데이터를 필요로 하면:

1. `analyzer.py::_fetch_page` 또는 데이터 추출 함수에서 새 필드 수집.
2. `analyzer.py::analyze_url`에서 `context` dict에 추가.
3. `rule_engine.py` 핸들러에서 `ctx["새_키"]`로 접근.

## 어드민 UI

3개 페이지 + 1개 reference:

| 페이지 | 역할 |
| :-- | :-- |
| **Audit 기준 설정** | 카테고리/항목/배점/룰 토글 편집. 저장은 `PUT /admin/config`. |
| **Audit List 관리** | URL 그룹 CRUD. `audit_data.json` 저장. |
| **정기 Audit 관리** | 그룹별 cron 스케줄. 현재는 UI만, 실행기는 추후 구현. |
| **Audit 기준 문서** | `docs/audit-criteria.md`를 마크다운 렌더 (read-only). |

어드민 활성: 환경변수 `ADMIN_PASSWORD` 설정 필요. 미설정 시 `/admin*` 모두 404.

## 개발 / 운영 명령어

```bash
# 로컬 실행 (어드민 포함)
ADMIN_PASSWORD=test python3 -m uvicorn main:app --reload --port 8765

# CSR 분석 활성화 (Playwright Chromium 설치)
pip3 install playwright playwright-stealth
python3 -m playwright install chromium

# 단위 테스트
python3 -m unittest tests.test_rule_engine -v

# 룰 타입 / 핸들러 정합성 빠른 검증
python3 -c "from rule_engine import RULE_TYPES, _HANDLERS, _ASYNC_HANDLERS; \
print(set(RULE_TYPES) ^ (set(_HANDLERS) | set(_ASYNC_HANDLERS)))"  # 빈 set 출력 = 일관됨
```

## 응답 스키마 (analyze API)

```jsonc
POST /analyze { "url": "...", "scope": "all" }
→ {
  "url": "...",
  "base_url": "...",
  "scope": "all",
  "pdp": { "is_pdp": bool, "path_segments": [...], "pattern": "...", "segment_count": int },
  "robots_txt": { "status": "found|not_found|error", "bots": {...}, "raw": "..." },
  "json_ld":    { "status": "found|not_found", "count": int, "schemas": [...], "all_types": [...], "raw_sources": [...], "raw": [...] },
  "csr_ratio":  { "status": "ok|skipped|unavailable", "ssr_chars": int, "csr_chars": int, "ratio": float|null },
  "score": {
    "total": int,
    "max": int,
    "grade": "Good|Need Improvement|Poor",
    "breakdown": {
      "performance":   { "points": int, "max": int, "passed": int, "total": int, "items": { "<id>": { "label": str, "pass": bool, "value": str|null, "hint": str|null, "rule_type": str } } },
      "accessibility": { ... },
      "seo":           { ... },
      "ai_readiness":  { ... }
    }
  }
}
```

## 알려진 제약 / TODO

- **PSI API**: LCP/CLS/INP는 enabled:false 상태. 활성화 시 `psi_metric` 룰 타입 신규 + Google API 키 + 분당 25회 rate limit 핸들링 필요.
- **PDP #38/#39 selector**: US(React/MUI) PDP 기준(`Product-*`, `img[src*=PDPGalleryThumbnail]`) + 구 AEM(`.c-*`) fallback 병기. 다른 국가가 별도 템플릿이면 어드민에서 selector 추가 필요.
- **Soft 404 (#43)**: 본문 길이 임계값(200자)은 도메인별 튜닝 필요할 수 있음.

## 정기 Audit 실행기 (구현됨)

- 모듈: `scheduler.py` (APScheduler `AsyncIOScheduler`)
- 트리거: `daily` (매일 HH:MM) · `weekly` (매주 월요일) · `monthly` (매월 1일)
- 결과: `audit_data.json::runs[]`에 누적, 최근 50개 보관
- 마이그레이션: 기존 index 기반 스키마(`groupIdx`, `freq`) 자동 변환 + 안정 ID 부여
- 어드민: 스케줄별 **"지금 실행"** + **"실행 이력"** 버튼 제공
- API:
  - `POST /admin/schedules/{id}/run` — 즉시 1회 실행
  - `GET  /admin/schedules/runs?schedule_id=...&limit=20` — 실행 이력 조회

## 다국가 sitemap (구현됨)

- `rule_engine.py::_detect_country_dir`: URL에서 국가 디렉토리(`/kr/`, `/us/`, `/en-us/` 등) 자동 감지
- `sitemap_recent` 룰: 국가 디렉토리 우선(`/<country>/sitemap.xml`, `/<country>/sitemap_index.xml`) → 도메인 루트 fallback(`/sitemap.xml`, `/sitemap_index.xml`)
- 어드민에서 룰 파라미터 `auto_country: yes/no`로 토글 가능

## 작업 시 주의사항

- **로컬에서 서버를 자동 실행하지 않는다.** 사용자가 `! <command>`로 직접 띄우는 게 기본. import 검증, 정적 분석은 가능.
- **factory default 동기화**: `scoring_config.json` 변경 시, 의도한 변경이 새 기본값이라면 `scoring_config.default.json`도 함께 업데이트.
- **룰/핸들러 일치**: 새 룰 추가 시 `RULE_TYPES`와 `_HANDLERS`/`_ASYNC_HANDLERS` 양쪽에 등록. `tests/test_rule_engine.py::TestRegistryConsistency`가 일치를 검증.
- **카테고리 추가 시**: `_calculate_score`의 `new_cat_keys`, 프론트의 `CATEGORY_META`/`renderCategoryCards`/`SCOPE_TO_CATS` 모두 업데이트.
- **민감정보**: `ADMIN_PASSWORD`는 환경변수만. `audit_data.json`은 git에 커밋해도 무방하나 운영 환경에서는 별도 영속 스토리지 권장.
