# GEO Agent Readability 검수 기준

> 6개 카테고리 41개 채점 항목 + 9월 감사 시행 예정 4항목.
> 점수·통과율은 제외한 **기준 정의 문서**입니다. 실측치는 Readability 대시보드에서 확인하세요.
> 원본: `data/readability/geo-agent-checklist.html` → `scripts/render-criteria.mjs` (별도 리포)
> 이 사본 갱신: 2026-08-28

## 카테고리

| 카테고리 | 채점 항목 | 무엇을 보는가 | scoring_config 키 |
| :-- | :-: | :-- | :-- |
| 사이트 성능 | 6 | 서버가 페이지를 얼마나 빠르고 안전하게 전달하는가 — 전송 계층 | `performance` |
| 웹접근성 | 4 | 사람과 기계가 문서 구조를 읽어낼 수 있는가 | `accessibility` |
| Basic SEO | 8 | 검색엔진이 페이지를 수집하고 표시할 수 있는가 | `seo` |
| 스키마마크업 | 10 | AI가 읽을 수 있는 구조화 데이터가 있는가 | `schema_markup` |
| 고인용 콘텐츠 | 5 | AI가 인용할 만한 서술이 본문에 있는가 | `citable_content` |
| AI Crawlability | 8 | AI 크롤러가 원문을 실제로 가져갈 수 있는가 | `ai_crawlability` |

> 총점은 카테고리 가중치 없이 `통과항목 / 전체항목 × 100`.

---

## 사이트 성능

### #1 — TTFB
- **정의**: 서버 요청 후 첫 번째 응답이 전달되기까지 걸리는 시간
- **PASS**: < 600ms
- **측정방법**: PageSpeed Insights(Lighthouse) `server-response-time`
- **check id**: `perf_ttfb`

### #2 — Compression
- **정의**: 페이지 전송 용량을 줄이기 위한 HTTP 응답 압축 적용 여부
- **PASS**: gzip/br/deflate · **측정방법**: Content-Encoding 헤더 · **check id**: `perf_compression`

### #3 — HTTP Protocol
- **정의**: 페이지 전송에 사용되는 HTTP 통신 프로토콜 버전
- **PASS**: HTTP/2 이상 · **측정방법**: Alt-Svc, :status 헤더 · **check id**: `perf_http_protocol`

### #4 — Cache-Control
- **정의**: 브라우저가 리소스를 일정 기간 저장·재사용할 수 있도록 하는 캐시 유효기간
- **PASS**: max-age 설정 (0 포함) · **측정방법**: Cache-Control 헤더 · **check id**: `perf_cache_control`

### #6 — Redirect Chain
- **정의**: 최종 페이지에 도달하기 전 거치는 URL 리다이렉트 횟수
- **PASS**: ≤ 1회 · **측정방법**: redirectChain 메타데이터 · **check id**: `perf_redirect`

### #7 — Mixed Content
- **정의**: HTTPS 페이지 내 비보안(HTTP) 리소스 포함 여부
- **PASS**: 0개 · **측정방법**: http:// 리소스 탐지 · **check id**: `perf_mixed_content`

### (예정) LCP · CLS · INP
- **PASS**: LCP ≤ 4,000ms / CLS ≤ 0.25 / INP ≤ 500ms
- **측정방법**: PageSpeed Insights (INP는 CrUX 실사용자 데이터)
- **상태**: 9월 감사부터 추가 시행 (데이터 추출 및 검증 진행중)

### (예정) Agentic Browsing
- **정의**: AI Agent와 상호작용하기 위해 사이트가 얼마나 잘 구성되어 있는지 (구글 베타)
- **측정방법**: CLS · llms.txt · 에이전트 접근성 항목 평가
- **상태**: 9월 감사부터 추가 시행. WebMCP audit 3종은 대상 페이지가 Origin Trial 토큰을
  서빙해야 평가되며, 2026-08-26 확인 시 www.lg.com 은 토큰·구현 모두 없어 N/A로 남는다.

---

## 웹접근성

| # | 항목 | PASS | 측정방법 | check id |
| :-: | :-- | :-- | :-- | :-- |
| #9 | Image Alt | 누락 0개 | img[alt] 체크 | `a11y_image_alt` |
| #10 | Semantic HTML | main + 랜드마크 3개 이상 | main, nav, header, footer, article, section, aside | `a11y_semantic` |
| #11 | Heading Hierarchy | 위반 0개 | h1 → h3 점프 등 탐지 | `a11y_heading_hier` |
| #12 | ARIA Labels | 누락 < 10% | button, input, a 접근성 텍스트 | `a11y_aria_labels` |

---

## Basic SEO

| # | 항목 | PASS | check id |
| :-: | :-- | :-- | :-- |
| #13 | Title | 존재 (30~60자) | `seo_title` |
| #14 | Meta Description | 존재 (120~160자) | `seo_meta_desc` |
| #15 | Canonical | self-referencing | `seo_canonical` |
| #16 | H1 | 정확히 1개 | `seo_h1` |
| #17 | Robots | Indexing 허용 | `seo_robots`, `seo_robots_hdr` |
| #18 | Open Graph | og:title + og:image | `seo_open_graph` |
| #19 | Sitemap | 1개월 내 최신화된 Sitemap XML 존재 | `seo_sitemap` |

> #17은 meta robots(`seo_robots`)와 X-Robots-Tag 헤더(`seo_robots_hdr`) 2개로 채점.

---

## 스키마마크업

> 공통 PASS 조건: JSON-LD 필수요소 모두 존재, 파싱 성공

| # | 스키마 | 필수 요소 | check id |
| :-: | :-- | :-- | :-- |
| #21 | BreadcrumbList | itemListElement, item, name, position | `ai_schema_breadcrumb` |
| #23 | FAQPage | mainEntity | `ai_schema_faq` |
| #24 | CollectionPage | itemList, ListItem | `ai_schema_collection` |
| #25 | Product 풀세트 | name, description, sku, brand, offers.price, offers.availability, aggregateRating.ratingValue, Review | `ai_schema_product`, `ai_schema_offer` |
| #26 | ImageObject | url, name, description, uploadDate | `ai_schema_image` |
| #27 | VideoObject | url, name, description, thumbnailUrl | `ai_schema_video` |
| #28 | HowTo | HowToSupply / HowToStep | `ai_schema_howto` |
| #29 | Article | headline, author, publisher, articleBody | `ai_schema_article` |
| #49 | WebSite | — (체크리스트 문서에 대응 행 없음) | `ai_schema_website` |

> #25는 Product와 Offer 2개 항목으로 채점되어 스키마마크업 카테고리는 총 10개.

---

## 고인용 콘텐츠

| # | 항목 | PASS | 측정방법 | check id |
| :-: | :-- | :-- | :-- | :-- |
| #32 | FAQ Block | 1개 이상 | FAQPage Schema, details/summary, Q&A 패턴 | `ai_faq_block` |
| #33 | Definition Paragraph | 1개 이상 | "X는 Y이다", dfn, abbr | `ai_definition` |
| #34 | Author/Source | 저자 또는 (출처+날짜) | **JSON-LD** `author` 또는 (`datePublished` + `publisher`/`sourceOrganization`/`source`) | `ai_author_source` |
| #35 | Summary Box | 1개 이상 | TL;DR, Key Takeaways, Highlights, Abstract | `ai_summary_box` |
| #36 | Citable Sentences | 밀도 ≥ 10% | 숫자, 연도, 통계, 연구 키워드 포함 문장 | `ai_citable` |

> **#34 주의**: HTML `meta author`·본문 byline은 판정에 **사용하지 않는다**. AI가 파싱 가능한
> 구조화 데이터를 요구하는 기준이다. 따라서 통과율 0%는 "저자 표기가 없다"가 아니라
> "JSON-LD에 author/발행정보가 없다"로 읽어야 하고, 개선 액션은 Article/NewsArticle 스키마에
> `author`·`datePublished`·`publisher`를 추가하는 것이다.
>
> #34는 byline 개념이 성립하는 `newsroom`·`buying_guide`·`content` page_type에만 적용되며,
> 그 외 페이지타입은 N/A로 분모에서 빠진다.

---

## AI Crawlability

| # | 항목 | PASS | 측정방법 | check id |
| :-: | :-- | :-- | :-- | :-- |
| #37 | (JS) HTML Text Ratio | 밀도 ≥ 60% | JS 렌더링 후 텍스트 대비 HTML Text 비중 | `ai_ssr_ratio` |
| #38 | (JS) HTML Resource | PDP 썸네일 1-3번째 이미지가 HTML에 존재 | PDP HTML 파싱 후 SSR 확인 | `ai_pdp_thumbnails` |
| #39 | (JS) 핵심 element | PDP 핵심 element가 HTML로 존재 | PDP HTML 파싱 후 SSR 확인 | `ai_core_element` |
| #40 | Image File Name | 브랜드명 포함 | 이미지 파일 이름 규칙 검증 | `ai_image_filename` |
| #41 | Status Code (200) | 200 반환 | Status Code | `ai_status_200` |
| #42 | Status Code (Soft 404) | 200 반환 + HTML Text Count 기준 이상 | Status Code 및 본문 길이 검증 | `ai_soft_404` |
| #43 | llms.txt | 존재 | 각 국가별 llms.txt 검증 | `ai_llms_txt` |
| #40* | Summary Content SSR | — (체크리스트 문서에 대응 행 없음) | | `ai_summary_ssr` |

---

## 예외 처리

### 채점에서 제외된 항목
- **#5 HTML < 100KB** — 측정은 정확하나 lg.com HTML 중앙값이 1,536KB라 실질 통과율 0.0%.
  통과 건의 대부분이 본문 0자인 빈 404 셸이라 지표 방향이 반대였음
- **#8 Render Blocking 0** — 통과율 2.3%로 변별력 없음
- **#44 Sitemap XML** — #19 Sitemap과 rule이 완전히 동일한 중복 (`ai_sitemap_domain`, `enabled: false`)
- **#20 Organization · #22 Speakable · #30 digitalDocument · #31 Recipe** — `enabled: false`

### 집계 대상에서 빠지는 페이지
- **B2B(사업자) · 프로모션/약관** — GEO 대상이 아니라 점수·통과율·URL 카운트 전부에서 제외
- **비-200 페이지** (404 · 500 · fetch 실패) — 전 체크가 cascade-FAIL 이라 개선 대상이 아님
- **분류불가(unknown) · 홈페이지(home)** — 측정 의미 없음
- **회사소개(about)** — GEO 검수 대상이 아니라 감사 자체를 하지 않음 (2026-08-28 결정)

### 측정 기준이 바뀐 항목
- **#1 TTFB** — 어딧 크롤러 자체 측정값이 동시 크롤 큐잉에 오염돼 실제보다 6~200배 크게 잡혔음
  (UK 크롤러 1,088ms vs PSI 11ms). PageSpeed Insights의 `server-response-time`을 정본으로 교체.
  임계값 600ms (2026-08-28 실측 1,462건 기준 통과율 96.1%, 중앙값 223ms)
- **#4 Cache-Control** — 원래 룰이 `no-cache`/`no-store`가 섞이면 `max-age` 값과 무관하게 즉시
  FAIL 처리했음. `max-age` 디렉티브가 설정돼 있으면(0 포함) 통과로 완화
- **#34 Author 또는 출처+날짜** — `newsroom`·`buying_guide`·`content` page_type에만 적용,
  그 외는 N/A (분모 제외)

### 문서 번호와 채점 항목이 1:1이 아닌 곳
- **#17 Robots** — `seo_robots`(meta) + `seo_robots_hdr`(X-Robots-Tag), 두 개로 채점
- **#25 Product 풀세트** — `ai_schema_product` + `ai_schema_offer`, 두 개로 채점
- **#49 Schema: WebSite · #40 Summary Content SSR** — 채점은 되지만 체크리스트 문서에 대응 행 없음

---

## 감사 대상 국가

전략 10국 + Global-Site, 총 11개.

| 코드 | 표시명 | 비고 |
| :-- | :-- | :-- |
| us, uk, de, es, ca, au, br, mx, in, vn | 국가 코드 대문자 | CA는 `ca_en`(영문)만. 불어(`ca_fr`)는 제외 |
| global | **Global-Site** | `lg.com/global/newsroom` — 전량 `newsroom` page_type |

> `newsroom` 은 Global 전용이다. 국가별 보도자료는 별도 page_type **`press_media`**
> (프레스앤미디어)로 분류한다 — 경로 명칭이 국가마다 다르다:
> `press-and-media`(CA/AU/MX/IN) · `press-media`(UK/BR) · `press-release`(US) · `newsroom`(DE).
>
> `newsroom` · `press_media` · `support_troubleshoot` 세 타입은 **발행일 내림차순**으로
> 100개를 뽑는다. 계속 새 문서가 나오는 타입이라 URL 정렬순으로 자르면 오래된 문서만
> 반복 감사하게 된다 (`reports/page_dates.json`).

감사는 **page_type별 최대 100개 샘플**이다. URL 목록은 사이트맵 + PLP 상품 API(Coveo)의
활성 제품을 합쳐 구성한다(`build_url_csv.py` → `plp_discover.py`).
