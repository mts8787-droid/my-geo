# AI Readability / GEO Audit 체크리스트 (48개)

> **출처**: [Google Sheets — AI Readability 체크리스트 및 Pass 기준](https://docs.google.com/spreadsheets/d/15hEMMFqizFM90WpbbdYmVC0trwwDpKnJMMRcm3VHYek/edit)
> **버전**: 2026-05-06 import
> **용도**: 전면 재구축되는 Audit 기능의 기준 문서. 각 항목별 **PASS / FAIL 판정 기준**을 명시한다.

---

## 카테고리 요약

| 카테고리 | 항목 수 | ID 범위 | PoC 추진 |
| :-- | :-: | :-: | :-: |
| Performance | 11 | #1 ~ #11 | 8 (Y) / 3 (N — PSI API) |
| Accessibility | 4 | #9 ~ #12 (재시작) | 4 (Y) |
| SEO | 7 | #13 ~ #19 | 7 (Y) |
| AI Readiness (GEO 핵심) | 26 | #20 ~ #45 | 25 (Y) / 1 (조건부) |
| **합계** | **48** | — | **44 / 48** |

> ⚠️ **ID 중복 주의**: 원본 스프레드시트에서 Accessibility 섹션은 #9부터 다시 매김(#9 Image Alt, #10 Semantic HTML, #11 Heading Hierarchy, #12 ARIA). Performance #9 LCP, #10 CLS, #11 INP와 ID가 겹친다. 본 문서는 원본 ID를 보존하되, 코드 구현 시 카테고리 prefix(`perf_`, `a11y_`, `seo_`, `ai_`)로 구분 권장.

---

## 1. Performance (11개)

### #1 — TTFB
- **PASS**: TTFB **< 600ms**
- **FAIL (NON-PASS)**: TTFB ≥ 600ms 이거나 측정 불가
- **측정 방법**: `Server-Timing`, `X-Response-Time` 응답 헤더 확인 (없으면 fetch round-trip 시간 측정)
- **측정 레벨**: 도메인
- **구현**: HTTP 응답에서 헤더 우선 사용, 없으면 `performance.now()` 기반 fallback
- **PoC**: Y

### #2 — Compression
- **PASS**: `Content-Encoding`이 **gzip / br / deflate** 중 하나
- **FAIL**: `Content-Encoding` 미설정 또는 그 외 값 (identity 등)
- **측정 방법**: HTTP 응답 헤더 `Content-Encoding` 값 확인
- **측정 레벨**: 페이지
- **구현**: `response.headers.get("content-encoding")` 정규화 후 화이트리스트 매칭
- **PoC**: Y

### #3 — HTTP Protocol
- **PASS**: HTTP/2 이상 (h2, h3)
- **FAIL**: HTTP/1.1 이하
- **측정 방법**: `Alt-Svc` 헤더, `:status` 의사 헤더 또는 클라이언트 protocol 정보
- **측정 레벨**: 도메인
- **구현**: httpx의 `response.http_version` 확인 또는 `Alt-Svc: h2`/`h3` 검출
- **PoC**: Y

### #4 — Cache-Control
- **PASS**: `Cache-Control` 헤더에 `max-age > 0`
- **FAIL**: 헤더 부재, `no-store`, `max-age=0`
- **측정 방법**: HTTP 응답 헤더 `Cache-Control` 파싱 → `max-age` 정수 추출
- **측정 레벨**: 페이지
- **구현**: 정규식 `max-age=(\d+)`로 추출, no-store/no-cache 디렉티브 검사
- **PoC**: Y

### #5 — HTML Size
- **PASS**: HTML 본문 크기 **< 100KB** (102,400 bytes)
- **FAIL**: 100KB 이상
- **측정 방법**: HTML 다운로드 후 `len(html.encode("utf-8"))` (Buffer.byteLength 등가)
- **측정 레벨**: 페이지
- **구현**: 응답 본문을 utf-8 인코딩한 바이트 길이 계산
- **PoC**: Y

### #6 — Redirect Chain
- **PASS**: 리다이렉트 **≤ 1회**
- **FAIL**: 2회 이상
- **측정 방법**: redirect 체인 메타데이터 (`response.history` 길이)
- **측정 레벨**: 페이지
- **구현**: httpx의 `len(response.history)` (Playwright는 `response.request().redirectedFrom()` 체인 추적)
- **PoC**: Y

### #7 — Mixed Content
- **PASS**: HTTPS 페이지 내 `http://` 리소스 **0개**
- **FAIL**: 1개 이상
- **측정 방법**: HTML 파싱 후 `img`, `script`, `link`, `iframe` 등의 `src`/`href` 속성에서 `http://` URL 정규식 탐지
- **측정 레벨**: 페이지
- **구현**: BeautifulSoup으로 해당 태그 수집 → 속성값 정규식 매칭 (페이지가 https인 경우만 적용)
- **PoC**: Y

### #8 — Render Blocking
- **PASS**: `<head>` 내 blocking script **0개** (defer/async 없는 외부 script)
- **FAIL**: 1개 이상
- **측정 방법**: `<head>` 내 `<script src>` 중 `defer`/`async` 속성 없는 것 카운트
- **측정 레벨**: 페이지
- **구현**: BS4로 `head > script[src]` 선택 후 속성 검사
- **PoC**: Y

### #9 — LCP (Largest Contentful Paint)
- **PASS**: LCP **≤ 4,000ms**
- **FAIL**: 4,000ms 초과 또는 측정 불가
- **측정 방법**: PageSpeed Insights API 응답값 (필드 데이터 우선)
- **측정 레벨**: 페이지
- **비고**: 구글 기준 'Needs Improvement' 이상 등급. Self-healing 제외 (수동 수정 필요)
- **구현**: PSI API 호출 (분당 25회 rate limit, API 키 필요, 배치 처리)
- **PoC**: 조건부 (현 단계에서 N)

### #10 — CLS (Cumulative Layout Shift)
- **PASS**: CLS **≤ 0.25**
- **FAIL**: 0.25 초과
- **측정 방법**: PageSpeed Insights API
- **측정 레벨**: 페이지
- **비고**: 구글 'Needs Improvement' 이상. Self-healing 제외
- **구현**: PSI API 호출
- **PoC**: 조건부 (N)

### #11 — INP (Interaction to Next Paint)
- **PASS**: INP **≤ 500ms**
- **FAIL**: 500ms 초과
- **측정 방법**: PageSpeed Insights API
- **측정 레벨**: 페이지
- **비고**: 구글 'Needs Improvement' 이상. Self-healing 제외
- **구현**: PSI API 호출
- **PoC**: 조건부 (N)

---

## 2. Accessibility (4개)

### #9 — Image Alt *(a11y)*
- **PASS**: alt 속성 누락 `<img>` **0개**
- **FAIL**: alt 누락 1개 이상 (빈 문자열 `alt=""`은 장식 이미지로 간주, PASS 처리)
- **측정 방법**: `<img>` 태그의 `alt` 속성 존재 여부
- **측정 레벨**: 페이지
- **구현**: BS4로 `img` 전체 선택 후 `alt is None` 카운트
- **PoC**: Y

### #10 — Semantic HTML *(a11y)*
- **PASS**: `<main>` 존재 **+** 랜드마크 태그 **3개 이상**
- **FAIL**: `<main>` 부재 또는 랜드마크 < 3개
- **측정 방법**: `main`, `nav`, `header`, `footer`, `article`, `section`, `aside` 카운트
- **측정 레벨**: 페이지
- **구현**: BS4로 각 태그 카운트 후 main 1개 + 그 외 합계 ≥ 3 검증
- **PoC**: Y

### #11 — Heading Hierarchy *(a11y)*
- **PASS**: 헤딩 레벨 점프 **0건** (h1→h2→h3 순)
- **FAIL**: 점프 1건 이상 (예: h1 다음 h3)
- **측정 방법**: 모든 h1–h6을 문서 순서대로 추출 후 인접 레벨 차이 검사
- **측정 레벨**: 페이지
- **구현**: 순차 탐색하며 `current_level - prev_level > 1` 발생 시 위반 카운트
- **PoC**: Y

### #12 — ARIA Labels *(a11y)*
- **PASS**: 인터랙티브 요소 중 접근성 텍스트 누락 비율 **< 10%**
- **FAIL**: 누락 비율 10% 이상
- **측정 방법**: `button`, `input`, `a` 중 `aria-label` / `aria-labelledby` / `title` / 가시 텍스트가 모두 없는 요소 비율
- **측정 레벨**: 페이지
- **구현**: 인터랙티브 요소 수집 후 4가지 텍스트 소스 중 어느 하나라도 있으면 PASS
- **PoC**: Y

---

## 3. SEO (7개)

### #13 — Title
- **PASS**: `<title>` 태그 존재 + 비어있지 않음
- **FAIL**: 태그 부재 또는 빈 문자열
- **측정 방법**: `<head><title>` 텍스트 추출
- **측정 레벨**: 페이지
- **구현**: BS4로 `head > title` 선택, `.text.strip()` 길이 > 0 체크
- **PoC**: Y

### #14 — Meta Description
- **PASS**: `<meta name="description">` 존재 + content 비어있지 않음
- **FAIL**: 부재 또는 빈 content
- **측정 방법**: `meta[name="description"]` content 속성 추출
- **측정 레벨**: 페이지
- **구현**: BS4 `find("meta", attrs={"name": "description"})` → content 검사
- **PoC**: Y

### #15 — Canonical
- **PASS**: `<link rel="canonical">` href가 **현재 URL과 동일** (self-referencing)
- **FAIL**: 부재 또는 다른 URL
- **측정 방법**: `link[rel="canonical"]` href 추출 후 현재 URL과 비교 (정규화: 프로토콜·트레일링 슬래시·쿼리 정렬)
- **측정 레벨**: 페이지
- **구현**: URL 정규화 함수로 양쪽 비교
- **PoC**: Y

### #16 — H1
- **PASS**: `<h1>` 태그 **정확히 1개**
- **FAIL**: 0개 또는 2개 이상
- **측정 방법**: `<h1>` 카운트
- **측정 레벨**: 페이지
- **구현**: `len(soup.find_all("h1"))` == 1
- **PoC**: Y

### #17 — Robots
- **PASS**: 인덱싱 허용 (meta robots / X-Robots-Tag 어디에도 `noindex` 없음)
- **FAIL**: meta robots 또는 X-Robots-Tag 헤더에 `noindex` 포함
- **측정 방법**: `meta[name="robots"]` content + `X-Robots-Tag` HTTP 헤더 파싱
- **측정 레벨**: 페이지
- **구현**: 두 출처 모두 검사하여 `noindex` 토큰 검출
- **PoC**: Y

### #18 — Open Graph
- **PASS**: `og:title` **AND** `og:image` 둘 다 존재
- **FAIL**: 둘 중 하나라도 부재
- **측정 방법**: `meta[property^="og:"]` 전체 추출 후 두 속성 존재 여부
- **측정 레벨**: 페이지
- **구현**: dict로 og 메타 수집 후 두 키 존재 검사
- **PoC**: Y

### #19 — Sitemap
- **PASS**: 각 국가별 `/sitemap.xml`이 존재 **AND** Last-Modified / `<lastmod>`가 **최근 1개월 이내**
- **FAIL**: 부재 또는 1개월 이상 미갱신
- **측정 방법**: `/sitemap.xml` HEAD/GET 후 `Last-Modified` 헤더 또는 XML 내 `<lastmod>` 파싱
- **측정 레벨**: 페이지 (도메인 레벨 점검)
- **구현**: 각 국가 디렉토리별 sitemap 위치 결정 → 날짜 비교
- **PoC**: Y

---

## 4. AI Readiness — GEO 핵심 (26개)

> JSON-LD 스키마 검증은 모두 동일 패턴: 페이지의 모든 `<script type="application/ld+json">` 파싱 → `@type` 매칭 → 필수 필드 존재 여부 검증.

### #20 — Schema: Organization
- **PASS**: `@type=Organization` 존재 **AND** `contactPoint`, `address`, `geo`, `hasMap` 모두 존재
- **FAIL**: 스키마 부재 또는 4개 필드 중 하나라도 누락
- **측정 레벨**: 페이지 (공통)
- **구현**: JSON-LD 파싱 → Organization 노드 추출 → 4 필드 체크
- **PoC**: Y

### #21 — Schema: BreadcrumbList
- **PASS**: `@type=BreadcrumbList` + `itemListElement` 배열 + 각 원소에 `item`, `name`, `position` 존재
- **FAIL**: 스키마 부재 또는 필수 필드 누락
- **측정 레벨**: 페이지 (공통)
- **구현**: JSON-LD에서 BreadcrumbList 추출 → 배열 원소별 검사
- **PoC**: Y

### #22 — Schema: Speakable
- **PASS**: `speakable` 객체에 `cssSelector` 또는 `xpath` 존재
- **FAIL**: 부재
- **측정 레벨**: 페이지 (공통)
- **구현**: 모든 JSON-LD 노드에서 `speakable` 키 탐색 후 selector 필드 검사
- **PoC**: Y

### #23 — Schema: FAQ
- **PASS**: `@type=FAQPage` + `mainEntity` 배열 길이 ≥ 1
- **FAIL**: 스키마 부재 또는 mainEntity 비어있음
- **측정 레벨**: 페이지 (공통)
- **구현**: JSON-LD → FAQPage → mainEntity 길이 체크
- **PoC**: Y

### #24 — Schema: CollectionPage *(PLP)*
- **PASS**: `@type=CollectionPage` + `mainEntity` 또는 `itemList` (`ListItem` 포함) 존재
- **FAIL**: 부재
- **측정 레벨**: 페이지 Type (본부콘텐츠 — PLP)
- **구현**: PLP에 한해 적용. JSON-LD에서 CollectionPage → itemList 검사
- **PoC**: Y

### #25 — Schema: Product + Offer + AggregateRating + Review *(PDP)*
- **PASS**: `@type=Product` + 다음 모두 존재 — `name`, `description`, `sku`, `brand`, `offers.price`, `offers.availability`, `aggregateRating.ratingValue`, `review`
- **FAIL**: 하나라도 누락
- **측정 레벨**: 페이지 Type (본부콘텐츠 — PDP)
- **구현**: Product 스키마에서 중첩 필드까지 모두 검증
- **PoC**: Y

### #26 — Schema: ImageObject
- **PASS**: `@type=ImageObject` + `url`, `name`, `description`, `uploadDate` 모두 존재
- **FAIL**: 부재 또는 필드 누락
- **측정 레벨**: 페이지 Type (본부콘텐츠 PDP/Micro Site, 고가혁 Support)
- **구현**: JSON-LD에서 ImageObject → 4 필드 체크
- **PoC**: Y

### #27 — Schema: VideoObject
- **PASS**: `@type=VideoObject` + `url`, `name`, `description`, `thumbnailUrl` 모두 존재
- **FAIL**: 누락
- **측정 레벨**: 페이지 Type (PDP/Micro Site/Support)
- **구현**: JSON-LD → VideoObject → 4 필드 체크
- **PoC**: Y

### #28 — Schema: HowTo *(Support)*
- **PASS**: `@type=HowTo` + `supply` (HowToSupply) **AND** `step` (HowToStep) 배열 존재
- **FAIL**: 누락
- **측정 레벨**: 페이지 Type (본부콘텐츠 Micro Site, 고가혁 Support)
- **구현**: HowTo → supply / step 배열 길이 검사
- **PoC**: Y

### #29 — Schema: Article *(Newsroom/Support)*
- **PASS**: `@type=Article` + `headline`, `author`, `publisher`, `articleBody` 모두 존재
- **FAIL**: 누락
- **측정 레벨**: 페이지 Type (PR Newsroom, 고가혁 Support)
- **구현**: Article → 4 필드 체크
- **PoC**: Y

### #30 — Schema: DigitalDocument *(Support)*
- **PASS**: `@type=DigitalDocument` + `name`, `url`, `fileFormat`, `description` 모두 존재
- **FAIL**: 누락
- **측정 레벨**: 페이지 Type (고가혁 Support)
- **구현**: DigitalDocument → 4 필드 체크
- **PoC**: Y

### #31 — Schema: Recipe *(Micro Site)*
- **PASS**: `@type=Recipe` + `name`, `description`, `image`, `author`, `datePublished`, `recipeIngredient`, `recipeInstructions` 모두 존재
- **FAIL**: 누락
- **측정 레벨**: 페이지 Type (본부콘텐츠 Micro Site)
- **구현**: Recipe → 7 필드 체크
- **PoC**: Y

### #32 — FAQ Block (콘텐츠)
- **PASS**: 페이지에 FAQ 블록 **1개 이상** — `<details>/<summary>` 또는 Q&A 패턴 (`dl>dt+dd`, `.faq-item` 등)
- **FAIL**: 0개
- **측정 레벨**: 페이지 Type
- **비고**: 추후 고도화 (콘텐츠 수정 영역)
- **구현**: BS4로 `details` 카운트 + Q&A 패턴 셀렉터 매칭
- **PoC**: Y

### #33 — Definition Paragraph
- **PASS**: 정의문 패턴 **1개 이상** — "X는 Y이다", "X란 Y를 말한다" 정규식 또는 `<dfn>` / `<abbr>` 태그
- **FAIL**: 0개
- **측정 레벨**: 페이지 Type
- **비고**: 추후 고도화
- **구현**: 본문 텍스트 정규식 매칭 + dfn/abbr 카운트
- **PoC**: Y

### #34 — Author / Source *(Newsroom)*
- **PASS**: 저자 정보 **OR** (출처 + 날짜) 존재 — `meta[name="author"]`, `.byline`, `datePublished` 등
- **FAIL**: 둘 다 부재
- **측정 레벨**: 페이지 Type (PR Newsroom)
- **구현**: 저자 셀렉터 우선 → 없으면 출처+날짜 동시 존재 검사
- **PoC**: Y

### #35 — Summary Box
- **PASS**: `TL;DR`, `Key Takeaways`, `Highlights`, `Abstract`, `Summary`, `요약` 등 키워드를 가진 블록 **1개 이상**
- **FAIL**: 0개
- **측정 레벨**: 페이지 Type
- **비고**: 추후 고도화
- **구현**: 텍스트/클래스/id에서 키워드 매칭
- **PoC**: Y

### #36 — Citable Sentences
- **PASS**: 인용 가능 문장 밀도 **≥ 10%** (전체 문장 대비)
- **FAIL**: 10% 미만
- **측정 방법**: 정규식으로 다음 패턴 포함 문장 카운트 — `\d+%`, `\d{4}년`, `\$[\d,]+`, `\d+배`, `에 따르면` 등
- **측정 레벨**: 페이지 Type
- **비고**: 이미 구현됨 (배포 필요)
- **구현**: 문장 분할 후 패턴 매칭 비율 계산
- **PoC**: Y

### #37 — JS HTML Text Ratio
- **PASS**: JS 렌더링 후 텍스트 대비 원본 HTML 텍스트 비율 **≥ 60%** (SSR 비중)
- **FAIL**: 60% 미만
- **측정 방법**: Playwright로 렌더 후 `document.body.innerText` 추출 → 원본 HTML 텍스트와 길이 비교
- **측정 레벨**: 페이지
- **구현**: SSR 텍스트 / 렌더 후 텍스트 비율
- **PoC**: Y

### #38 — JS HTML Resource (PDP 썸네일)
- **PASS**: PDP 썸네일 이미지 **1–3번째**가 **원본 HTML**에 `<img>` 태그로 존재 (SSR)
- **FAIL**: 1–3번째 중 하나라도 원본 HTML 부재 (CSR로만 로드)
- **측정 레벨**: 페이지 (PDP)
- **구현**: 페이지 fetch로 받은 raw HTML에서 썸네일 셀렉터로 1–3번째 img 검사
- **PoC**: Y

### #39 — JS 핵심 Element 체크
- **PASS**: PDP의 핵심 element (제품 카드 등)가 원본 HTML에 SSR로 존재
- **FAIL**: 부재
- **측정 레벨**: 페이지 Type (본부콘텐츠 — PDP)
- **비고**: 핵심 element CSS selector 정의 필요 — **LG팀과 협의 후 확정**
- **구현**: 정의 후 셀렉터 매칭. 현재는 spec 미확정
- **PoC**: 조건부 (LG 정의 필요)

### #40 — Summary Content SSR
- **PASS**: 페이지 타입별 Summary 컴포넌트가 원본 HTML에 존재
- **FAIL**: CSR로만 렌더 또는 부재
- **측정 레벨**: 페이지
- **비고**: 추후 고도화 (콘텐츠 수정 영역)
- **구현**: 페이지 타입별 Summary selector 매핑 후 raw HTML에서 검사
- **PoC**: Y

### #41 — Image File Name
- **PASS**: PDP 이미지 파일명에 **브랜드/제품명** 키워드 포함
- **FAIL**: 미포함 (예: `IMG_1234.jpg`)
- **측정 방법**: `<img src>` URL의 파일명 부분에서 LG, 제품명 등 키워드 매칭
- **측정 레벨**: 페이지
- **구현**: URL parsing → basename → 키워드 정규식
- **PoC**: Y

### #42 — Status Code (200)
- **PASS**: HTTP 응답 status code **200**
- **FAIL**: 그 외 (3xx 리다이렉트, 4xx, 5xx)
- **측정 레벨**: 페이지
- **구현**: `response.status_code == 200`
- **PoC**: Y

### #43 — Soft 404
- **PASS**: status 200 **AND** HTML 텍스트 길이가 임계값 이상 (정상 페이지)
- **FAIL**: status 200이지만 텍스트 길이 미달 (실제로는 빈 페이지/에러 메시지)
- **측정 방법**: status code + HTML body text 길이 임계값 비교 (임계값 정의 필요)
- **측정 레벨**: 도메인 레벨 (국가 디렉토리별)
- **구현**: 200 응답 후 `soup.get_text()` 길이 < threshold 검출
- **PoC**: Y

### #44 — llms.txt / llms-corepage.txt
- **PASS**: 각 국가별 `/llms.txt` (또는 `/llms-corepage.txt`) HEAD 요청 status **200**
- **FAIL**: 부재 (404 등)
- **측정 레벨**: 도메인 레벨 (국가 디렉토리별)
- **구현**: 각 국가 base URL에서 HEAD 요청
- **PoC**: Y

### #45 — Sitemap XML *(도메인 레벨)*
- **PASS**: 각 국가별 sitemap.xml 존재 **AND** `lastmod` 또는 `Last-Modified` **최근 1개월 이내**
- **FAIL**: 부재 또는 1개월 이상 미갱신
- **측정 레벨**: 도메인 레벨 (국가 디렉토리별)
- **구현**: 국가별 sitemap.xml 파싱 → 날짜 검증 (#19와 유사하나 도메인 레벨)
- **PoC**: Y

---

## PASS / FAIL 판정 종합 매트릭스

| # | 항목 | PASS 한 줄 요약 | 측정 레벨 | PoC |
| :-: | :-- | :-- | :-: | :-: |
| 1 | TTFB | < 600ms | 도메인 | Y |
| 2 | Compression | gzip/br/deflate | 페이지 | Y |
| 3 | HTTP Protocol | HTTP/2+ | 도메인 | Y |
| 4 | Cache-Control | max-age > 0 | 페이지 | Y |
| 5 | HTML Size | < 100KB | 페이지 | Y |
| 6 | Redirect Chain | ≤ 1회 | 페이지 | Y |
| 7 | Mixed Content | http:// 0개 | 페이지 | Y |
| 8 | Render Blocking | head blocking script 0 | 페이지 | Y |
| 9 | LCP | ≤ 4,000ms | 페이지 | N |
| 10 | CLS | ≤ 0.25 | 페이지 | N |
| 11 | INP | ≤ 500ms | 페이지 | N |
| 9 (a11y) | Image Alt | 누락 0 | 페이지 | Y |
| 10 (a11y) | Semantic HTML | main + 랜드마크 ≥ 3 | 페이지 | Y |
| 11 (a11y) | Heading Hierarchy | 점프 위반 0 | 페이지 | Y |
| 12 (a11y) | ARIA Labels | 누락 < 10% | 페이지 | Y |
| 13 | Title | 존재 | 페이지 | Y |
| 14 | Meta Description | 존재 | 페이지 | Y |
| 15 | Canonical | self-referencing | 페이지 | Y |
| 16 | H1 | 정확히 1개 | 페이지 | Y |
| 17 | Robots | indexing 허용 | 페이지 | Y |
| 18 | Open Graph | og:title + og:image | 페이지 | Y |
| 19 | Sitemap (page) | 1개월 내 갱신 | 페이지 | Y |
| 20 | Org schema | contactPoint+address+geo+hasMap | 페이지 | Y |
| 21 | BreadcrumbList | itemListElement 모두 | 페이지 | Y |
| 22 | Speakable | cssSelector 존재 | 페이지 | Y |
| 23 | FAQ schema | mainEntity ≥ 1 | 페이지 | Y |
| 24 | CollectionPage | itemList 존재 | 페이지 Type (PLP) | Y |
| 25 | Product 풀세트 | 8 필드 모두 | 페이지 Type (PDP) | Y |
| 26 | ImageObject | 4 필드 모두 | 페이지 Type | Y |
| 27 | VideoObject | 4 필드 모두 | 페이지 Type | Y |
| 28 | HowTo | supply + step | 페이지 Type | Y |
| 29 | Article | 4 필드 모두 | 페이지 Type | Y |
| 30 | DigitalDocument | 4 필드 모두 | 페이지 Type | Y |
| 31 | Recipe | 7 필드 모두 | 페이지 Type | Y |
| 32 | FAQ Block | 1개 이상 | 페이지 Type | Y |
| 33 | Definition Paragraph | 1개 이상 | 페이지 Type | Y |
| 34 | Author/Source | 저자 또는 출처+날짜 | 페이지 Type | Y |
| 35 | Summary Box | 키워드 1개 이상 | 페이지 Type | Y |
| 36 | Citable Sentences | 밀도 ≥ 10% | 페이지 Type | Y |
| 37 | JS HTML Text Ratio | SSR ≥ 60% | 페이지 | Y |
| 38 | JS HTML Resource | 1–3 썸네일 SSR | 페이지 | Y |
| 39 | JS 핵심 Element | 핵심 SSR | 페이지 Type | 조건부 |
| 40 | Summary SSR | Summary HTML 존재 | 페이지 | Y |
| 41 | Image File Name | 브랜드 키워드 포함 | 페이지 | Y |
| 42 | Status 200 | == 200 | 페이지 | Y |
| 43 | Soft 404 | 200 + 텍스트 임계 이상 | 도메인 | Y |
| 44 | llms.txt | 200 응답 | 도메인 | Y |
| 45 | Sitemap (domain) | 1개월 내 갱신 | 도메인 | Y |

---

## 비PoC 항목 (3개)

PageSpeed Insights API에 의존하는 Core Web Vitals 3종은 PoC 단계에서 제외:
- **#9 LCP**, **#10 CLS**, **#11 INP** — PSI API rate limit (분당 25회), API 키 발급/배치 처리 필요

self-healing 대상에서도 제외 (수동 수정 필요).

## 정의 필요 항목 (1개)

- **#39 핵심 Element** — PDP의 "핵심 element" CSS selector를 LG팀과 협의하여 확정 후 구현

---

## 다음 단계

1. ✅ 본 문서를 어드민의 audit 기준 reference로 노출
2. 룰 엔진(`rule_engine.py`)에 새 룰 타입 정의 (헤더 검사, 스키마 필드 검증, JS 렌더링 비교 등)
3. 카테고리별 점수 가중치 결정 (현재 `scoring_config.json`에 정의된 max 값 재조정)
4. `analyzer.py`에서 각 항목별 데이터 수집 함수 구현
5. 어드민 UI에서 본 문서를 기준으로 룰 활성/비활성 토글 가능하게 연결
