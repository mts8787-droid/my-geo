"""page_type별 CSR/SSR 표본 baseline 생성 + 조회.

Why: 정기 sitemap audit는 lightweight=True 모드로 동작하여 CSR/SSR ratio를 측정하지
않는다 (Playwright 메모리 부담 회피). 대신 매월 page_type별 10개 URL을 무작위
표본 추출하여 Playwright(lightweight=False)로 실측, 그 평균을 baseline으로 저장.
일반 분석 시 같은 page_type의 다른 URL에는 이 baseline 값을 채워준다.

데이터 형식 (data/csr_baseline.json):
{
  "<page_type_id>": {
    "avg_ratio":   0.523,            # SSR/CSR ratio 평균
    "avg_score":   7.0,              # 0~10 점수 평균
    "tier":        "good",           # 빈도 최다 tier
    "avg_ssr_chars": 3500,           # 참고용
    "avg_csr_chars": 6800,
    "sample_size": 10,
    "sample_urls": ["...", "..."],   # 어떤 URL로 측정했는지 (디버그용)
    "updated_at":  "2026-..."
  },
  ...
}
"""

import asyncio
import json
import logging
import random
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

BASELINE_PATH = Path(__file__).parent / "data" / "csr_baseline.json"

SAMPLES_PER_TYPE = 10
# Playwright 인스턴스 메모리 무거움 — 동시 2개로 매우 보수적 (Render 500MB)
MAX_CONCURRENCY = 2

# 차단 페이지 감지 — Akamai "Access Denied" 같은 짧은 차단 페이지를 baseline에 넣지 않도록.
# 정상 LG 페이지의 SSR/CSR 가시 텍스트는 보통 1000자 이상. 1000자 미만이면 차단으로 간주.
_BLOCKED_PAGE_CHAR_THRESHOLD = 1000

# PDP/PLP 휴리스틱 — page_types.json에 url_pattern이 없어서 unknown으로 분류되는 페이지
# 후보를 잡기 위한 정규식. 후보는 이후 httpx fetch + body class 검사로 실제 검증된다.
# PDP: /<country>/<category>/<model-code>/  (model-code에 숫자 포함, 알파벳+숫자 혼합)
_PDP_HEURISTIC = re.compile(r"^https?://[^/]+/[a-z]{2}/[a-z][a-z0-9-]+/[a-z0-9-]*[0-9][a-z0-9-]*/?$", re.I)
# PLP: /<country>/<category>/   (1-depth, 숫자/하이픈 없거나 단순)
_PLP_HEURISTIC = re.compile(r"^https?://[^/]+/[a-z]{2}/[a-z][a-z0-9-]+/?$", re.I)

_BODY_CLASS_PDP = {"productpage", "pdp"}
_BODY_CLASS_PLP = {"plppage", "categorypage", "productlistingpage"}
_META_TEMPLATE_PDP = {"pdp-page", "product-page"}
_META_TEMPLATE_PLP = {"plp-page", "category-page", "product-listing-page"}

# 표본 다양성 — PDP 표본은 아래 필수 카테고리에서 각 1개 이상 보장 + 나머지 무작위
# 패턴은 LG SG URL 기준 (다른 국가는 후속 확장).
_PDP_REQUIRED_CATEGORIES = {
    "TV":      re.compile(r"/(tvs|tvs-soundbars)/", re.I),
    "OLED":    re.compile(r"oled", re.I),
    "세탁기":   re.compile(r"/(washing-machines|laundry)/", re.I),
    "냉장고":   re.compile(r"/(refrigerators|fridge-freezers)/", re.I),
    "에어컨":   re.compile(r"/(residential-air-conditioner|air-conditioners?)/", re.I),
    "에어케어":  re.compile(r"/(air-purifiers|aerotower-aerofurniture|air-care)/", re.I),
}

# URL 제외 패턴 — test 페이지, 그 외 비프로덕션 흔적
_EXCLUDE_URL_PATTERN = re.compile(r"/(test|adobeqa|sandbox|preview|staging|dev|local)\b", re.I)


async def _verify_url_page_type(client, url: str) -> Optional[str]:
    """httpx로 URL fetch → body class + meta template으로 'pdp', 'plp' 또는 None 반환.

    PDP/PLP 휴리스틱 후보 검증용. body class가 잘 잡히는 게 가장 신뢰성 높음.
    리다이렉트된 URL은 제외 (원본 URL이 더 이상 유효하지 않은 신호).
    """
    try:
        r = await client.get(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; GEOAudit/1.0)",
            "Accept": "text/html",
        }, timeout=15, follow_redirects=True)
        if r.status_code != 200:
            return None
        # 리다이렉트가 발생했으면 (r.history 비어있지 않음) 제외 — 다른 URL로 보내는 페이지
        if r.history:
            return None
        html = r.text
    except Exception:
        return None

    # body class 추출
    m = re.search(r'<body\b[^>]*\bclass\s*=\s*"([^"]+)"', html, re.I)
    if m:
        classes = {c.lower() for c in m.group(1).split()}
        if classes & _BODY_CLASS_PDP:
            return "pdp"
        if classes & _BODY_CLASS_PLP:
            return "plp"

    # meta template fallback
    m = re.search(r'<meta\s+name="template"\s+content="([^"]+)"', html, re.I)
    if m:
        tmpl = m.group(1).strip().lower()
        if tmpl in _META_TEMPLATE_PDP:
            return "pdp"
        if tmpl in _META_TEMPLATE_PLP:
            return "plp"

    return None


def _pick_pdp_samples(verified_urls: list, n: int = SAMPLES_PER_TYPE) -> list:
    """PDP 검증 풀에서 필수 카테고리(TV/OLED/세탁기/냉장고/에어컨/에어케어) 각 1개 이상 보장
    + 부족분은 무작위로 채워 총 n개 반환.
    """
    samples = []
    used = set()
    # 1. 필수 카테고리에서 1개씩 (있는 만큼)
    for cat_name, pattern in _PDP_REQUIRED_CATEGORIES.items():
        matches = [u for u in verified_urls if pattern.search(u) and u not in used]
        if matches:
            pick = random.choice(matches)
            samples.append(pick)
            used.add(pick)
    # 2. 부족분 무작위
    remaining = [u for u in verified_urls if u not in used]
    extra = max(0, n - len(samples))
    if extra and remaining:
        samples.extend(random.sample(remaining, min(extra, len(remaining))))
    return samples


async def _augment_pdp_plp_samples(all_urls: list, existing_samples: dict) -> dict:
    """unknown 분류된 URL에서 PDP/PLP 후보 휴리스틱 추출 → httpx 검증 → 검증된 풀에서
    SAMPLES_PER_TYPE개 무작위 추출하여 existing_samples에 채워준다.

    검증은 동시 10개 (httpx 가벼움). 차단/오류 URL은 자연 제외.
    """
    import httpx

    # 휴리스틱 후보 수집 + test/staging 등 제외
    def _ok(u):
        return not _EXCLUDE_URL_PATTERN.search(u)
    pdp_candidates = [u for u in all_urls if _PDP_HEURISTIC.search(u) and _ok(u)]
    plp_candidates = [u for u in all_urls if _PLP_HEURISTIC.search(u) and _ok(u)]

    # PDP는 카테고리 다양성을 위해 더 많이 검증 (필수 카테고리 풀 확보용)
    # PLP는 후보 자체가 적음
    PREFETCH_LIMIT_PDP = 120  # 필수 6개 카테고리 + 부족분 모집
    PREFETCH_LIMIT_PLP = 60
    if len(pdp_candidates) > PREFETCH_LIMIT_PDP:
        pdp_candidates = random.sample(pdp_candidates, PREFETCH_LIMIT_PDP)
    if len(plp_candidates) > PREFETCH_LIMIT_PLP:
        plp_candidates = random.sample(plp_candidates, PREFETCH_LIMIT_PLP)

    sem = asyncio.Semaphore(10)

    async with httpx.AsyncClient() as client:
        async def _verify(url):
            async with sem:
                return url, await _verify_url_page_type(client, url)

        candidates_all = pdp_candidates + plp_candidates
        results = await asyncio.gather(*(_verify(u) for u in candidates_all))

    verified = {"pdp": [], "plp": []}
    for url, pt in results:
        if pt in verified:
            verified[pt].append(url)

    log.info("PDP/PLP 휴리스틱 검증: pdp 후보=%d / 확인=%d, plp 후보=%d / 확인=%d",
             len(pdp_candidates), len(verified["pdp"]),
             len(plp_candidates), len(verified["plp"]))

    # PDP는 카테고리 다양성 보장 picker 사용. PLP는 무작위.
    if verified["pdp"]:
        existing_samples["pdp"] = _pick_pdp_samples(verified["pdp"], SAMPLES_PER_TYPE)
    if verified["plp"]:
        n = min(SAMPLES_PER_TYPE, len(verified["plp"]))
        existing_samples["plp"] = random.sample(verified["plp"], n)

    return existing_samples


def get_baseline_for_page_type(page_type_id: str) -> Optional[dict]:
    """page_type id에 대한 baseline 반환. 없으면 None."""
    if not page_type_id or not BASELINE_PATH.exists():
        return None
    try:
        with open(BASELINE_PATH, encoding="utf-8") as f:
            baseline = json.load(f)
        return baseline.get(page_type_id)
    except Exception:
        return None


def load_baseline_all() -> dict:
    """전체 baseline dict 반환 (admin 조회용)."""
    if not BASELINE_PATH.exists():
        return {}
    try:
        with open(BASELINE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


async def regenerate_baseline() -> dict:
    """모든 그룹의 URL을 page_type별로 분류 후 type당 SAMPLES_PER_TYPE개 무작위 추출 →
    Playwright(lightweight=False) 분석 → 평균을 csr_baseline.json에 저장.

    Returns: {"status": "ok", "types": N, "sample_total": M, "took_sec": ...}
    """
    from analyzer import analyze_url
    from page_type import detect_page_type
    import audit_store

    started_at = datetime.now(timezone.utc)

    try:
        data = await audit_store.load()
    except Exception as e:
        log.exception("audit_store 로드 실패: %s", e)
        return {"status": "error", "error": f"audit_store 로드 실패: {e}"}

    all_urls = []
    for g in data.get("groups", []):
        all_urls.extend(g.get("urls", []) or [])
    if not all_urls:
        return {"status": "no_urls", "types": 0, "sample_total": 0}

    # page_type별 분류 — soup 없이 URL pattern만으로 (sampling 목적이라 충분)
    by_type = defaultdict(list)
    for url in all_urls:
        try:
            pt = detect_page_type(None, url)
            type_id = pt.get("id") or "unknown"
        except Exception:
            type_id = "unknown"
        by_type[type_id].append(url)

    # type별 추출 — unknown 제외, 각 type 최대 SAMPLES_PER_TYPE개
    samples = {}
    for type_id, urls in by_type.items():
        if type_id == "unknown":
            continue
        n = min(SAMPLES_PER_TYPE, len(urls))
        if n > 0:
            samples[type_id] = random.sample(urls, n)

    # PDP/PLP는 page_types.json url_pattern이 없어 unknown으로 분류됨 — 휴리스틱+검증으로 보강
    try:
        samples = await _augment_pdp_plp_samples(all_urls, samples)
    except Exception as e:
        log.warning("PDP/PLP 휴리스틱 검증 실패 (skip): %s", e)

    if not samples:
        return {"status": "no_samples", "types": 0, "sample_total": 0}

    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def _analyze_one(url: str) -> Optional[dict]:
        async with sem:
            try:
                # Playwright 포함 풀 분석
                result = await analyze_url(url, lightweight=False)
                csr = result.get("csr_ratio") or {}
                if csr.get("status") != "ok":
                    return None
                ssr_chars = csr.get("ssr_chars") or 0
                csr_chars = csr.get("csr_chars") or 0
                # 차단 페이지 감지 — 양쪽 다 임계값 미만이면 Akamai 차단으로 간주, 표본 제외
                if ssr_chars < _BLOCKED_PAGE_CHAR_THRESHOLD or csr_chars < _BLOCKED_PAGE_CHAR_THRESHOLD:
                    log.warning("baseline %s: 차단으로 간주 (ssr=%d, csr=%d) — 표본 제외",
                                url, ssr_chars, csr_chars)
                    return None
                return {
                    "url":       url,
                    "ratio":     csr.get("ratio"),
                    "tier":      csr.get("tier"),
                    "score":     csr.get("score"),
                    "ssr_chars": ssr_chars,
                    "csr_chars": csr_chars,
                }
            except Exception as e:
                log.warning("baseline 분석 실패 %s: %s", url, e)
                return None

    # type별 분석 → 평균 산출
    baseline = {}
    sample_total = 0
    for type_id, urls in samples.items():
        results = await asyncio.gather(*[_analyze_one(u) for u in urls])
        valid = [r for r in results if r and r.get("ratio") is not None]
        if not valid:
            continue
        n = len(valid)
        sample_total += n
        avg_ratio = sum(r["ratio"] for r in valid) / n
        avg_score = sum((r["score"] or 0) for r in valid) / n
        avg_ssr   = sum((r["ssr_chars"] or 0) for r in valid) / n
        avg_csr   = sum((r["csr_chars"] or 0) for r in valid) / n
        tier_count = Counter(r["tier"] for r in valid)
        most_tier = tier_count.most_common(1)[0][0] if tier_count else "unknown"
        baseline[type_id] = {
            "avg_ratio":     round(avg_ratio, 3),
            "avg_score":     round(avg_score, 1),
            "tier":          most_tier,
            "avg_ssr_chars": int(avg_ssr),
            "avg_csr_chars": int(avg_csr),
            "sample_size":   n,
            "sample_urls":   [r["url"] for r in valid],
            "updated_at":    datetime.now(timezone.utc).isoformat(),
        }

    # 저장
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(BASELINE_PATH, "w", encoding="utf-8") as f:
        json.dump(baseline, f, ensure_ascii=False, indent=2)

    took = (datetime.now(timezone.utc) - started_at).total_seconds()
    return {
        "status": "ok",
        "types": len(baseline),
        "sample_total": sample_total,
        "took_sec": round(took, 1),
    }
