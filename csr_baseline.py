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
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

BASELINE_PATH = Path(__file__).parent / "data" / "csr_baseline.json"

SAMPLES_PER_TYPE = 10
# Playwright 인스턴스 메모리 무거움 — 동시 2개로 매우 보수적 (Render 500MB)
MAX_CONCURRENCY = 2


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
                return {
                    "url":       url,
                    "ratio":     csr.get("ratio"),
                    "tier":      csr.get("tier"),
                    "score":     csr.get("score"),
                    "ssr_chars": csr.get("ssr_chars"),
                    "csr_chars": csr.get("csr_chars"),
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
