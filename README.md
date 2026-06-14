# GEO Audit Tool

**Generative Engine Optimization(생성 엔진 최적화) 감사 도구**

URL을 입력하면 해당 사이트가 GPT, Gemini, Claude 등 AI 엔진에 얼마나 잘 최적화되어 있는지 즉시 분석합니다.

## 분석 항목

4개 카테고리로 점검합니다. 점수는 **활성 항목 중 PASS 비율(%)** 로 산정하며, 페이지 타입과 매칭되지 않는 항목은 N/A 처리되어 분모에서 제외됩니다.

| 카테고리 (key) | 활성/전체 항목 | 대표 점검 |
|------|:------:|------|
| Performance (`performance`) | 8 / 11 | TTFB, 압축, HTTP/2+, 캐시 헤더, 이미지 최적화 등 (LCP/CLS/INP는 PSI API 필요로 비활성) |
| Accessibility (`accessibility`) | 4 / 4 | Image Alt, Semantic HTML, Heading 계층, ARIA |
| SEO (`seo`) | 8 / 8 | Title, Meta Description, Canonical, robots(meta+헤더), Open Graph, sitemap 등 |
| AI Readiness (`ai_readiness`) | 28 / 34 | JSON-LD 스키마(페이지 타입별), llms.txt, FAQ, 요약 박스, 통계, SSR/CSR 비중, robots AI 봇 등 |

### 스키마(JSON-LD) 채점 — 페이지 타입별

스키마 항목은 **해당 페이지 타입에 매칭될 때만 채점**됩니다 (`applies_to_page_types`). 예: Product/Offer/FAQ → PDP, CollectionPage/ItemList → PLP, NewsArticle → newsroom. 매칭되지 않는 스키마는 N/A로 분모에서 제외됩니다.

- **활성**: BreadcrumbList(범용), FAQPage, CollectionPage, Product, ImageObject, VideoObject, HowTo, Article, WebSite, Offer, ItemList, NewsArticle, Person, AggregateRating/Review
- **비활성**: Organization(#20), Speakable(#22), DigitalDocument(#30), Recipe(#31), AboutPage(#41), WebPage(#42)

### 등급 기준

| 등급 | 점수 |
|------|------|
| Good | ≥ 80 |
| Need Improvement | ≥ 60 |
| Poor | < 60 |

> 채점 기준의 source of truth는 [`docs/audit-criteria.md`](docs/audit-criteria.md) + `scoring_config.json` 입니다. 항목/배점/룰은 어드민에서 동적으로 토글할 수 있습니다.

## 기술 스택

- **Backend**: Python FastAPI + uvicorn
- **Frontend**: HTML + Tailwind CSS (CDN)
- **HTTP 클라이언트**: httpx (비동기)
- **HTML 파싱**: BeautifulSoup4
- **브라우저 엔진**: Playwright (CSR 분석용)
- **채점 시스템**: 룰 엔진 기반 (어드민에서 동적 관리)

## AI 모델 사용 가이드

| 용도 | 모델 | 모델 ID |
|------|------|---------| 
| 개발 (코드 작성/리팩토링/디버깅) | Claude Opus | `claude-opus-4-7` |
| 운영 (코드 리뷰/모니터링/경량 작업) | Claude Sonnet | `claude-sonnet-4-6` |

## 설치 및 실행

### 1. Python 설치

[python.org](https://www.python.org/downloads/) 에서 Python 3.11+ 설치
(설치 시 "Add Python to PATH" 체크 필수)

### 2. 의존성 설치

```bash
pip install -r requirements.txt
```

### 3. 서버 실행

```bash
python main.py
```

브라우저에서 http://localhost:8000 접속

## 프로젝트 구조

```
my-geo-audit/
├── main.py                # FastAPI 앱 진입점 (API 라우팅, 보안, Rate Limit)
├── analyzer.py            # GEO 분석 핵심 로직 (페이지 fetch, JSON-LD, CSR 분석)
├── rule_engine.py         # 룰 엔진 — 어드민 정의 규칙 평가 (34종 룰 타입)
├── csr_local.py           # 로컬 SSR/CSR 분석 CLI
├── scoring_config.json    # 채점 설정 파일 (어드민에서 수정 가능)
├── requirements.txt       # Python 의존성
├── build.sh               # Render 배포용 빌드 스크립트
├── render.yaml            # Render 서비스 설정
├── static/
│   ├── index.html         # 프론트엔드 (분석 UI)
│   └── admin.html         # 어드민 (채점 기준/그룹/스케줄 관리)
└── extension/
    ├── manifest.json      # Chrome 확장 매니페스트 (MV3)
    ├── popup.html         # 확장 프로그램 UI
    ├── popup.js           # 확장 프로그램 로직
    └── icons/             # 확장 프로그램 아이콘
```

## 배포

Render에서 자동 배포됩니다:

```bash
# 빌드: bash build.sh
# 시작: python -m uvicorn main:app --host 0.0.0.0 --port $PORT
```

환경 변수:
- `PORT` — 서버 포트 (기본: 8000)
- `ALLOWED_ORIGINS` — CORS 허용 도메인 (쉼표 구분)
- `ADMIN_PASSWORD` — 어드민 비밀번호 (미설정 시 어드민 비활성화)
