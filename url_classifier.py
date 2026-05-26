"""사이트맵 그룹의 모든 URL을 httpx fetch + page_type 분류 → 결과 저장.

목적: page_type 분류를 URL pattern만이 아니라 실제 HTML 소스(body class + meta
template)까지 봐서 정확히 분류. 200 응답 + non-redirect URL만 채택.

결과 형식 (data/url_classifications.json):
{
  "<group_id>": {
    "group_name": "...",
    "updated_at": "...",
    "took_sec": 600.5,
    "summary": {
      "total":       4275,
      "classified":  3500,   # 200 + 분류 성공
      "failed":      775,    # 4xx/5xx/redirect/timeout
      "by_type":     {"home": 1, "pdp": 245, ...}
    },
    "classifications": {
      "<url>": {"page_type": "pdp", "matched_by": "body_class=productpage", "http_status": 200}
    },
    "failed_urls": [
      {"url": "...", "reason": "404"}, ...
    ]
  }
}
"""

import asyncio
import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

CLASSIFICATIONS_PATH = Path(__file__).parent / "data" / "url_classifications.json"

# 동시 fetch 수 — httpx는 가벼우니 20 정도 안전
CLASSIFY_CONCURRENCY = 20
FETCH_TIMEOUT_SEC = 15

# 200 응답 + non-redirect 만 채택 (사용자 명시)
# redirect URL은 분류 제외 (원본 URL이 유효하지 않다는 신호)


async def _classify_one(client, url: str) -> dict:
    """단일 URL 분류 — 결과 dict 반환.

    성공: {"url", "page_type", "matched_by", "http_status"}
    실패: {"url", "failed": True, "reason"}
    """
    from page_type import detect_page_type
    from bs4 import BeautifulSoup

    try:
        r = await client.get(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; GEOAudit/1.0; +https://geoaudit.dev)",
            "Accept": "text/html",
            "Accept-Language": "en;q=0.9",
        }, timeout=FETCH_TIMEOUT_SEC, follow_redirects=True)
    except Exception as e:
        return {"url": url, "failed": True, "reason": f"fetch_error: {type(e).__name__}"}

    if r.status_code != 200:
        return {"url": url, "failed": True, "reason": f"http_{r.status_code}"}
    if r.history:
        return {"url": url, "failed": True, "reason": "redirect"}

    ctype = r.headers.get("content-type", "")
    if "text/html" not in ctype.lower():
        return {"url": url, "failed": True, "reason": f"non-html ({ctype[:30]})"}

    # HTML parse → page_type 검출
    try:
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        return {"url": url, "failed": True, "reason": f"parse_error: {e}"}

    try:
        pt = detect_page_type(soup, url)
    except Exception as e:
        return {"url": url, "failed": True, "reason": f"detect_error: {e}"}

    return {
        "url":         url,
        "page_type":   pt.get("id") or "unknown",
        "matched_by":  pt.get("matched_by") or "",
        "http_status": r.status_code,
    }


async def classify_group(group_id: str) -> dict:
    """지정 그룹의 모든 URL을 분류하여 data/url_classifications.json에 저장.

    Returns: {"status": "ok", "group_id", "total", "classified", "failed", "took_sec"}
    """
    import audit_store
    import httpx

    started_at = datetime.now(timezone.utc)

    try:
        data = await audit_store.load()
    except Exception as e:
        return {"status": "error", "error": f"audit_store 로드 실패: {e}"}

    group = next((g for g in data.get("groups", []) if g.get("id") == group_id), None)
    if not group:
        return {"status": "error", "error": f"group not found: {group_id}"}
    urls = group.get("urls") or []
    if not urls:
        return {"status": "no_urls", "group_id": group_id}

    sem = asyncio.Semaphore(CLASSIFY_CONCURRENCY)

    # httpx async client — limits 같이 잡음
    limits = httpx.Limits(max_connections=CLASSIFY_CONCURRENCY * 2,
                          max_keepalive_connections=CLASSIFY_CONCURRENCY)

    classified = {}
    failed = []

    async with httpx.AsyncClient(limits=limits) as client:
        async def _one(url):
            async with sem:
                return await _classify_one(client, url)

        # gather가 4000개 task 한 번에 시작해도 sem이 동시 20개로 제한
        results = await asyncio.gather(*(_one(u) for u in urls))

    for r in results:
        if r.get("failed"):
            failed.append({"url": r["url"], "reason": r["reason"]})
        else:
            classified[r["url"]] = {
                "page_type":   r["page_type"],
                "matched_by":  r["matched_by"],
                "http_status": r["http_status"],
            }

    # by_type 분포 + summary
    by_type = Counter(c["page_type"] for c in classified.values())

    took = (datetime.now(timezone.utc) - started_at).total_seconds()
    summary = {
        "total":      len(urls),
        "classified": len(classified),
        "failed":     len(failed),
        "by_type":    dict(by_type.most_common()),
    }
    group_entry = {
        "group_name":      group.get("name", group_id),
        "updated_at":      datetime.now(timezone.utc).isoformat(),
        "took_sec":        round(took, 1),
        "summary":         summary,
        "classifications": classified,
        "failed_urls":     failed[:200],  # 너무 길어지지 않게 200개로 제한 (전수는 summary.failed)
    }

    # 기존 데이터 merge
    CLASSIFICATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CLASSIFICATIONS_PATH.exists():
        try:
            with open(CLASSIFICATIONS_PATH, encoding="utf-8") as f:
                full = json.load(f)
        except Exception:
            full = {}
    else:
        full = {}
    full[group_id] = group_entry
    with open(CLASSIFICATIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(full, f, ensure_ascii=False, indent=2)

    return {
        "status":     "ok",
        "group_id":   group_id,
        "total":      len(urls),
        "classified": len(classified),
        "failed":     len(failed),
        "by_type":    dict(by_type.most_common()),
        "took_sec":   round(took, 1),
    }


def load_classifications_all() -> dict:
    """전체 그룹의 분류 결과 dict 반환 (admin 조회용)."""
    if not CLASSIFICATIONS_PATH.exists():
        return {}
    try:
        with open(CLASSIFICATIONS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_group_classification(group_id: str) -> Optional[dict]:
    """지정 그룹의 분류 결과만 반환."""
    return load_classifications_all().get(group_id)
