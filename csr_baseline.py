"""group(sitemap)별·page_type별 CSR/SSR 표본 baseline.

Why: 정기 sitemap audit는 lightweight=True 모드로 동작해 CSR/SSR ratio를 측정하지
않는다 (Playwright 메모리 부담 회피). 대신 각 sitemap 그룹별로 page_type 10개 URL을
무작위 표본 추출해 Playwright(lightweight=False)로 실측, 그 평균을 baseline으로 저장.
일반 분석에서 같은 그룹/page_type의 다른 URL에는 이 평균값을 주입한다.

데이터 형식 (data/csr_baseline.json):
{
  "<group_id>": {                          # 예: "grp_lg_sg", "grp_lg_uk"
    "_meta": {
      "group_name":   "LG Sitemap - sg",
      "updated_at":   "2026-...",
      "took_sec":     1800,
      "sample_total": 60
    },
    "<page_type_id>": {                    # 예: "home", "pdp"
      "avg_ratio":     0.52,
      "avg_score":     7.0,
      "tier":          "good",
      "avg_ssr_chars": 9966,
      "avg_csr_chars": 19163,
      "sample_size":   10,
      "sample_urls":   [...],
      "updated_at":    "2026-..."
    }, ...
  }, ...
}

조회 우선순위 (get_baseline_for_page_type):
  1) group_id 특정 → 해당 그룹의 page_type entry
  2) fallback → 모든 그룹의 해당 page_type 평균 (다른 국가 데이터로라도 추정)
"""

import asyncio
import json
import logging
import os
import random
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

BASELINE_PATH = Path(__file__).parent / "data" / "csr_baseline.json"

SAMPLES_PER_TYPE = 10
# Render 500MB 환경에서 Playwright 동시 인스턴스 메모리 안전 — 1로 매우 보수적.
# 한 번 측정 ~30분/그룹 (page_type별 10개 × 보통 6~10 page_type).
MAX_CONCURRENCY = 1

# 차단 페이지 감지 — Akamai "Access Denied" 같은 짧은 페이지를 baseline에 넣지 않도록.
# 정상 LG 페이지의 SSR/CSR 가시 텍스트는 보통 1000자 이상.
_BLOCKED_PAGE_CHAR_THRESHOLD = 1000

# 요청 간 딜레이(초) — 연속 측정 시 Akamai velocity 차단 회피. env로 조정.
BASELINE_DELAY_SEC = float(os.getenv("BASELINE_DELAY_SEC", "0"))

# PDP/PLP 휴리스틱 — page_types.json에 url_pattern이 없어서 unknown으로 분류되는 페이지.
_PDP_HEURISTIC = re.compile(r"^https?://[^/]+/[a-z]{2}/[a-z][a-z0-9-]+/[a-z0-9-]*[0-9][a-z0-9-]*/?$", re.I)
_PLP_HEURISTIC = re.compile(r"^https?://[^/]+/[a-z]{2}/[a-z][a-z0-9-]+/?$", re.I)

_BODY_CLASS_PDP = {"productpage", "pdp"}
_BODY_CLASS_PLP = {"plppage", "categorypage", "productlistingpage"}
_META_TEMPLATE_PDP = {"pdp-page", "product-page"}
_META_TEMPLATE_PLP = {"plp-page", "category-page", "product-listing-page"}

# 표본 다양성 — PDP는 아래 필수 카테고리에서 1개씩 보장 + 나머지 무작위.
_PDP_REQUIRED_CATEGORIES = {
    "TV":      re.compile(r"/(tvs|tvs-soundbars)/", re.I),
    "OLED":    re.compile(r"oled", re.I),
    "세탁기":   re.compile(r"/(washing-machines|laundry)/", re.I),
    "냉장고":   re.compile(r"/(refrigerators|fridge-freezers)/", re.I),
    "에어컨":   re.compile(r"/(residential-air-conditioner|air-conditioners?)/", re.I),
    "에어케어":  re.compile(r"/(air-purifiers|aerotower-aerofurniture|air-care)/", re.I),
}

# URL 제외 — test/staging/preview 등 비프로덕션 흔적.
_EXCLUDE_URL_PATTERN = re.compile(
    r"(?<![a-z0-9])(test(ing)?[0-9_-]*|adobeqa|sandbox|preview|staging|dev|local|business)(?![a-z])", re.I)


# ── 조회 ─────────────────────────────────────────────────────────────────

def _detect_group_id_from_url(url: str) -> Optional[str]:
    """URL의 국가 코드로 group_id 추정 (예: /sg/ → grp_lg_sg)."""
    m = re.search(r"https?://[^/]+/([a-z]{2})/", url)
    return f"grp_lg_{m.group(1)}" if m else None


def get_baseline_for_page_type(page_type_id: str, group_id: Optional[str] = None) -> Optional[dict]:
    """주어진 page_type에 대한 baseline entry 반환.

    우선순위:
      1) group_id 지정되고 해당 그룹에 entry 있으면 그것 사용 (가장 정확)
      2) 다른 그룹의 동일 page_type entry 평균 (fallback, _fallback=True 표시)
      3) None
    """
    if not page_type_id or not BASELINE_PATH.exists():
        return None
    try:
        with open(BASELINE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    # 1) 그룹별 우선
    if group_id and group_id in data:
        entry = data[group_id].get(page_type_id)
        if entry:
            return entry

    # 2) fallback — 모든 그룹의 동일 page_type entry 평균
    all_entries = []
    for gid, gd in data.items():
        if isinstance(gd, dict):
            entry = gd.get(page_type_id)
            if entry and isinstance(entry, dict) and entry.get("avg_ratio") is not None:
                all_entries.append(entry)
    if not all_entries:
        return None
    n = len(all_entries)
    return {
        "avg_ratio":     round(sum(e.get("avg_ratio", 0) for e in all_entries) / n, 3),
        "avg_score":     round(sum(e.get("avg_score", 0) for e in all_entries) / n, 1),
        "tier":          Counter(e.get("tier") for e in all_entries).most_common(1)[0][0],
        "avg_ssr_chars": int(sum(e.get("avg_ssr_chars", 0) for e in all_entries) / n),
        "avg_csr_chars": int(sum(e.get("avg_csr_chars", 0) for e in all_entries) / n),
        "sample_size":   sum(e.get("sample_size", 0) for e in all_entries),
        "_fallback":     True,
        "_fallback_groups": n,
    }


def load_baseline_all() -> dict:
    """전체 baseline dict 반환 (admin 조회용)."""
    if not BASELINE_PATH.exists():
        return {}
    try:
        with open(BASELINE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ── 검증 ─────────────────────────────────────────────────────────────────

async def _verify_url_page_type(client, url: str) -> Optional[str]:
    """httpx fetch → body class/meta template으로 'pdp', 'plp' 또는 None.
    리다이렉트 발생 URL 제외.
    """
    try:
        r = await client.get(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; GEOAudit/1.0)",
            "Accept": "text/html",
        }, timeout=15, follow_redirects=True)
        if r.status_code != 200 or r.history:
            return None
        html = r.text
    except Exception:
        return None
    m = re.search(r'<body\b[^>]*\bclass\s*=\s*"([^"]+)"', html, re.I)
    if m:
        classes = {c.lower() for c in m.group(1).split()}
        if classes & _BODY_CLASS_PDP: return "pdp"
        if classes & _BODY_CLASS_PLP: return "plp"
    m = re.search(r'<meta\s+name="template"\s+content="([^"]+)"', html, re.I)
    if m:
        tmpl = m.group(1).strip().lower()
        if tmpl in _META_TEMPLATE_PDP: return "pdp"
        if tmpl in _META_TEMPLATE_PLP: return "plp"
    return None


def _pick_pdp_samples(verified_urls: list, n: int = SAMPLES_PER_TYPE) -> list:
    """PDP 검증 풀에서 필수 카테고리 1개씩 보장 + 나머지 무작위."""
    samples = []
    used = set()
    for cat_name, pattern in _PDP_REQUIRED_CATEGORIES.items():
        matches = [u for u in verified_urls if pattern.search(u) and u not in used]
        if matches:
            pick = random.choice(matches)
            samples.append(pick)
            used.add(pick)
    remaining = [u for u in verified_urls if u not in used]
    extra = max(0, n - len(samples))
    if extra and remaining:
        samples.extend(random.sample(remaining, min(extra, len(remaining))))
    return samples


async def _augment_pdp_plp_samples(all_urls: list, existing_samples: dict) -> dict:
    """unknown 분류 URL에서 PDP/PLP 후보 휴리스틱 → httpx 검증 → 표본 추출."""
    import httpx
    def _ok(u): return not _EXCLUDE_URL_PATTERN.search(u)
    pdp_candidates = [u for u in all_urls if _PDP_HEURISTIC.search(u) and _ok(u)]
    plp_candidates = [u for u in all_urls if _PLP_HEURISTIC.search(u) and _ok(u)]

    PREFETCH_LIMIT_PDP, PREFETCH_LIMIT_PLP = 120, 60
    if len(pdp_candidates) > PREFETCH_LIMIT_PDP:
        pdp_candidates = random.sample(pdp_candidates, PREFETCH_LIMIT_PDP)
    if len(plp_candidates) > PREFETCH_LIMIT_PLP:
        plp_candidates = random.sample(plp_candidates, PREFETCH_LIMIT_PLP)

    sem = asyncio.Semaphore(10)
    async with httpx.AsyncClient() as client:
        async def _verify(url):
            async with sem:
                return url, await _verify_url_page_type(client, url)
        results = await asyncio.gather(*(_verify(u) for u in (pdp_candidates + plp_candidates)))

    verified = {"pdp": [], "plp": []}
    for url, pt in results:
        if pt in verified:
            verified[pt].append(url)
    log.info("PDP/PLP 검증: pdp 후보=%d/확인=%d, plp 후보=%d/확인=%d",
             len(pdp_candidates), len(verified["pdp"]), len(plp_candidates), len(verified["plp"]))

    if verified["pdp"]:
        existing_samples["pdp"] = _pick_pdp_samples(verified["pdp"], SAMPLES_PER_TYPE)
    if verified["plp"]:
        n = min(SAMPLES_PER_TYPE, len(verified["plp"]))
        existing_samples["plp"] = random.sample(verified["plp"], n)
    return existing_samples


# ── 생성 ─────────────────────────────────────────────────────────────────

async def regenerate_baseline_for_group(group_id: str) -> dict:
    """지정 그룹의 URL만 가지고 baseline 생성/갱신.

    group의 URLs를 page_type별로 분류 → type당 SAMPLES_PER_TYPE 무작위 추출 →
    Playwright(lightweight=False) 실측 → 평균을 csr_baseline.json[group_id]에 저장.
    """
    from analyzer import analyze_url
    from page_type import detect_page_type
    import audit_store
    import db

    started_at = datetime.now(timezone.utc)

    try:
        data = audit_store.load()  # sync 함수
    except Exception as e:
        db.add_system_log(f"[baseline] {group_id}: audit_store 로드 실패 — {e}")
        return {"status": "error", "error": f"audit_store 로드 실패: {e}"}

    group = next((g for g in data.get("groups", []) if g.get("id") == group_id), None)
    if not group:
        db.add_system_log(f"[baseline] {group_id}: group not found")
        return {"status": "error", "error": f"group not found: {group_id}"}
    urls = group.get("urls") or []
    if not urls:
        db.add_system_log(f"[baseline] {group_id}: 그룹에 URL 없음 — skip")
        return {"status": "no_urls", "group_id": group_id}

    db.add_system_log(
        f"[baseline] {group_id}: 시작 — total URLs={len(urls)}, "
        f"samples_per_type={SAMPLES_PER_TYPE}, concurrency={MAX_CONCURRENCY}"
    )

    # page_type별 분류 (URL pattern 기반 — soup 없이)
    by_type = defaultdict(list)
    for url in urls:
        if _EXCLUDE_URL_PATTERN.search(url):
            continue
        try:
            pt = detect_page_type(None, url)
            type_id = pt.get("id") or "unknown"
        except Exception:
            type_id = "unknown"
        by_type[type_id].append(url)

    # type별 추출 (unknown 제외)
    samples = {}
    for type_id, type_urls in by_type.items():
        if type_id == "unknown":
            continue
        n = min(SAMPLES_PER_TYPE, len(type_urls))
        if n > 0:
            samples[type_id] = random.sample(type_urls, n)

    # PDP/PLP 보강 (휴리스틱+검증)
    try:
        samples = await _augment_pdp_plp_samples(urls, samples)
    except Exception as e:
        log.warning("PDP/PLP 휴리스틱 검증 실패 (skip): %s", e)

    if not samples:
        db.add_system_log(f"[baseline] {group_id}: 검증된 sample 0개 — skip")
        return {"status": "no_samples", "group_id": group_id}

    by_type_str = ", ".join(f"{k}:{len(v)}" for k, v in samples.items())
    db.add_system_log(f"[baseline] {group_id}: 표본 추출 완료 — {by_type_str}")

    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def _analyze_one(url: str) -> Optional[dict]:
        async with sem:
            try:
                if BASELINE_DELAY_SEC > 0:
                    await asyncio.sleep(BASELINE_DELAY_SEC)
                result = await analyze_url(url, lightweight=False)
                csr = result.get("csr_ratio") or {}
                if csr.get("status") != "ok":
                    log.warning("baseline %s: csr status=%s (%s) — 제외",
                                url, csr.get("status"), (csr.get("error") or "")[:120])
                    return None
                ssr_chars = csr.get("ssr_chars") or 0
                csr_chars = csr.get("csr_chars") or 0
                if ssr_chars < _BLOCKED_PAGE_CHAR_THRESHOLD or csr_chars < _BLOCKED_PAGE_CHAR_THRESHOLD:
                    log.warning("baseline %s: 차단 page (ssr=%d csr=%d) — 제외",
                                url, ssr_chars, csr_chars)
                    return None
                return {
                    "url": url, "ratio": csr.get("ratio"), "tier": csr.get("tier"),
                    "score": csr.get("score"),
                    "ssr_chars": ssr_chars, "csr_chars": csr_chars,
                }
            except Exception as e:
                log.warning("baseline 분석 실패 %s: %s", url, e)
                return None

    # 부분 저장 헬퍼 — page_type 한 개 끝날 때마다 디스크에 저장 (도중 죽어도 보존)
    def _save_partial(gb: dict, sample_total: int):
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        if BASELINE_PATH.exists():
            try:
                with open(BASELINE_PATH, encoding="utf-8") as f:
                    full = json.load(f)
            except Exception:
                full = {}
        else:
            full = {}
        # 그룹 entry merge (in_progress=True)
        entry = dict(gb)
        entry["_meta"] = {
            "group_name":   group.get("name", group_id),
            "updated_at":   datetime.now(timezone.utc).isoformat(),
            "took_sec":     round((datetime.now(timezone.utc) - started_at).total_seconds(), 1),
            "sample_total": sample_total,
            "types":        [k for k in gb.keys()],
            "in_progress":  True,
        }
        full[group_id] = entry
        with open(BASELINE_PATH, "w", encoding="utf-8") as f:
            json.dump(full, f, ensure_ascii=False, indent=2)

    # type별 분석 + 평균 (각 type 끝나면 부분 저장)
    group_baseline = {}
    sample_total = 0
    for type_id, type_urls in samples.items():
        db.add_system_log(f"[baseline] {group_id}: {type_id} 분석 시작 ({len(type_urls)}개)")
        try:
            results = await asyncio.gather(*[_analyze_one(u) for u in type_urls])
        except Exception as e:
            db.add_system_log(f"[baseline] {group_id}: {type_id} 분석 실패 — {e}")
            continue
        valid = [r for r in results if r and r.get("ratio") is not None]
        if not valid:
            db.add_system_log(f"[baseline] {group_id}: {type_id} — 유효 sample 0 (차단 추정), skip")
            continue
        n = len(valid)
        sample_total += n
        group_baseline[type_id] = {
            "avg_ratio":     round(sum(r["ratio"] for r in valid) / n, 3),
            "avg_score":     round(sum((r["score"] or 0) for r in valid) / n, 1),
            "tier":          Counter(r["tier"] for r in valid).most_common(1)[0][0],
            "avg_ssr_chars": int(sum((r["ssr_chars"] or 0) for r in valid) / n),
            "avg_csr_chars": int(sum((r["csr_chars"] or 0) for r in valid) / n),
            "sample_size":   n,
            "sample_urls":   [r["url"] for r in valid],
            "updated_at":    datetime.now(timezone.utc).isoformat(),
        }
        # 부분 저장 — 다음 type 시작 전
        _save_partial(group_baseline, sample_total)
        db.add_system_log(
            f"[baseline] {group_id}: {type_id} 완료 — ratio={group_baseline[type_id]['avg_ratio']*100:.1f}% "
            f"tier={group_baseline[type_id]['tier']} ({n}/{len(type_urls)} valid)"
        )

    took = (datetime.now(timezone.utc) - started_at).total_seconds()
    types_completed = [k for k in group_baseline.keys()]
    group_baseline["_meta"] = {
        "group_name":   group.get("name", group_id),
        "updated_at":   datetime.now(timezone.utc).isoformat(),
        "took_sec":     round(took, 1),
        "sample_total": sample_total,
        "types":        types_completed,
        "in_progress":  False,
    }

    # 기존 데이터 merge — 해당 group_id 키만 덮어씀
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if BASELINE_PATH.exists():
        try:
            with open(BASELINE_PATH, encoding="utf-8") as f:
                full = json.load(f)
        except Exception:
            full = {}
    else:
        full = {}
    full[group_id] = group_baseline
    with open(BASELINE_PATH, "w", encoding="utf-8") as f:
        json.dump(full, f, ensure_ascii=False, indent=2)

    db.add_system_log(
        f"[baseline] {group_id}: 완료 — types={len(types_completed)} samples={sample_total} took={took:.0f}s"
    )

    # peer(Mac Mini)로 즉시 push — Render ephemeral disk 보존용
    try:
        from main import _peer_push_aux
        await _peer_push_aux("/admin/csr-baseline-raw", full)
    except Exception as e:
        log.warning("peer push 실패 (무시): %s", e)
        db.add_system_log(f"[baseline] {group_id}: peer push 실패 (무시) — {e}")

    return {
        "status": "ok", "group_id": group_id,
        "types": len(types_completed),
        "sample_total": sample_total,
        "took_sec": round(took, 1),
    }


async def regenerate_baseline_all_groups() -> dict:
    """모든 그룹 순회 — 시간 오래 걸림 (1 그룹 ~30분 × N 그룹). 보통은 그룹별 trigger 권장.
    """
    import audit_store
    data = await audit_store.load()
    groups = [g for g in data.get("groups", []) if g.get("urls")]
    results = {}
    for g in groups:
        gid = g.get("id")
        log.info("baseline 갱신 시작: %s", gid)
        results[gid] = await regenerate_baseline_for_group(gid)
    return {"status": "ok", "groups": results}


# 기존 이름 호환 (scheduler.py 등이 호출)
async def regenerate_baseline() -> dict:
    """deprecated: 매월 cron에서 호출. 모든 그룹 처리 — 시간 오래 걸림.

    실용적으로는 admin UI에서 그룹별로 trigger 권장.
    """
    return await regenerate_baseline_all_groups()
