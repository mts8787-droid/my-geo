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
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

CLASSIFICATIONS_PATH = Path(__file__).parent / "data" / "url_classifications.json"

# 동시 fetch 수 / 요청 간 딜레이 — Akamai velocity 차단 시 env로 낮춰서 재시도
CLASSIFY_CONCURRENCY = int(os.getenv("CLASSIFY_CONCURRENCY", "20"))
CLASSIFY_DELAY_SEC = float(os.getenv("CLASSIFY_DELAY_SEC", "0"))
FETCH_TIMEOUT_SEC = 15

# Akamai 화이트리스트 UA (Render IP + 이 UA 조합으로 등록됨) — analyzer와 동일해야 함
try:
    from analyzer import _DEDICATED_UA as _CLASSIFY_UA
except Exception:
    _CLASSIFY_UA = "MyGEOAudit/1.0 (Audit agent operated by D2C Digital Marketing Team, LG Electronics)"
_CLASSIFY_UA = os.getenv("AUDIT_USER_AGENT") or _CLASSIFY_UA

# 200 응답 + non-redirect 만 채택 (사용자 명시)
# redirect URL은 분류 제외 (원본 URL이 유효하지 않다는 신호)

# URL 제외 패턴 — classify 대상에서 사전 제외.
# - test/staging/preview 등 비프로덕션
# - business: B2B 페이지는 GEO/소비자 audit 대상 아님 (사용자 명시 2026-05-27)
# (?<![a-z0-9]) — 세그먼트 시작뿐 아니라 -/_ 뒤도 잡음 (MW-Test, Testing_Folder 등)
_EXCLUDE_URL_PATTERN = re.compile(
    r"(?<![a-z0-9])(test(ing)?[0-9_-]*|adobeqa|sandbox|preview|staging|dev|local|business)(?![a-z])",
    re.IGNORECASE,
)


async def _classify_one(client, url: str) -> dict:
    """단일 URL 분류 — 결과 dict 반환.

    성공: {"url", "page_type", "matched_by", "http_status"}
    실패: {"url", "failed": True, "reason"}
    """
    from page_type import detect_page_type
    from bs4 import BeautifulSoup

    try:
        r = await client.get(url, headers={
            "User-Agent": _CLASSIFY_UA,
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

    Chunk 단위(CHUNK_SIZE)로 처리하여 매 chunk 완료 시:
      - 진행 로그(db.add_system_log) — admin 실시간 로그에 표시됨
      - 부분 결과 디스크 저장 — 도중 task 죽어도 거기까지 보존

    Returns: {"status": "ok", "group_id", "total", "classified", "failed", "took_sec"}
    """
    import audit_store
    import httpx
    import db

    CHUNK_SIZE = 500   # 한 chunk 처리에 ~75초 (500 / 20 동시 * 3초). 부분 저장 주기.

    started_at = datetime.now(timezone.utc)

    try:
        data = audit_store.load()
    except Exception as e:
        return {"status": "error", "error": f"audit_store 로드 실패: {e}"}

    group = next((g for g in data.get("groups", []) if g.get("id") == group_id), None)
    if not group:
        return {"status": "error", "error": f"group not found: {group_id}"}
    urls = group.get("urls") or []
    # URL이 dict 형식인 경우(audit_data에 따라) string으로 변환
    url_strs = []
    for u in urls:
        if isinstance(u, dict):
            us = u.get("url")
            if us: url_strs.append(us)
        elif isinstance(u, str):
            url_strs.append(u)
    urls = url_strs

    # EXCLUDE 필터 — business/test/staging 등 제외
    before = len(urls)
    urls = [u for u in urls if not _EXCLUDE_URL_PATTERN.search(u)]
    excluded_count = before - len(urls)

    if not urls:
        return {"status": "no_urls", "group_id": group_id, "excluded": excluded_count}

    # Resume — 기존 분류 결과 로드, 이미 처리된 URL은 skip.
    # Render 백그라운드가 도중에 죽어도 trigger 반복으로 점진 완성 가능.
    existing = load_classifications_all().get(group_id, {})
    classified = dict(existing.get("classifications", {}))
    failed = list(existing.get("failed_urls", []))
    failed_url_set = {f.get("url") for f in failed if isinstance(f, dict)}
    already_done = set(classified.keys()) | failed_url_set
    urls_to_process = [u for u in urls if u not in already_done]

    if not urls_to_process:
        db.add_system_log(f"[classify] {group_id}: 이미 모두 분류됨 ({len(classified)}/{len(urls)}) — skip")
        return {
            "status":     "already_done",
            "group_id":   group_id,
            "total":      len(urls),
            "classified": len(classified),
            "failed":     len(failed),
        }

    db.add_system_log(
        f"[classify] {group_id}: 시작 — total={len(urls)} "
        f"(excluded {excluded_count}, resume: 기존 {len(already_done)} skip, 남은 {len(urls_to_process)}) "
        f"chunk={CHUNK_SIZE} concurrency={CLASSIFY_CONCURRENCY}"
    )

    sem = asyncio.Semaphore(CLASSIFY_CONCURRENCY)
    limits = httpx.Limits(max_connections=CLASSIFY_CONCURRENCY * 2,
                          max_keepalive_connections=CLASSIFY_CONCURRENCY)

    def _save_partial(is_final: bool = False):
        """현재까지 결과를 디스크에 저장."""
        by_type = Counter(c["page_type"] for c in classified.values())
        elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
        group_entry = {
            "group_name":      group.get("name", group_id),
            "updated_at":      datetime.now(timezone.utc).isoformat(),
            "took_sec":        round(elapsed, 1),
            "in_progress":     not is_final,
            "summary": {
                "total":      len(urls),
                "classified": len(classified),
                "failed":     len(failed),
                "by_type":    dict(by_type.most_common()),
            },
            "classifications": classified,
            "failed_urls":     failed[:200],
        }
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
        return full

    async with httpx.AsyncClient(limits=limits) as client:
        async def _one(url):
            async with sem:
                if CLASSIFY_DELAY_SEC > 0:
                    await asyncio.sleep(CLASSIFY_DELAY_SEC)
                return await _classify_one(client, url)

        # CHUNK_SIZE 단위로 처리 — urls_to_process만 새로 처리, classified/failed에 누적
        for start in range(0, len(urls_to_process), CHUNK_SIZE):
            end = min(start + CHUNK_SIZE, len(urls_to_process))
            chunk = urls_to_process[start:end]
            results = await asyncio.gather(*(_one(u) for u in chunk))
            chunk_403 = 0
            for r in results:
                if r.get("failed"):
                    if r.get("reason") == "http_403":
                        # Akamai 차단은 일시적 — failed로 저장하지 않아 resume 시 재시도됨
                        chunk_403 += 1
                        continue
                    failed.append({"url": r["url"], "reason": r["reason"]})
                else:
                    classified[r["url"]] = {
                        "page_type":   r["page_type"],
                        "matched_by":  r["matched_by"],
                        "http_status": r["http_status"],
                    }
            # 중간 저장 + 진행 로그
            _save_partial(is_final=False)
            db.add_system_log(
                f"[classify] {group_id}: {len(classified)+len(failed)}/{len(urls)} 진행 — "
                f"classified={len(classified)} failed={len(failed)} "
                f"(이번 batch: {end}/{len(urls_to_process)})"
            )
            # 한 chunk의 절반 이상이 403이면 velocity 차단 발동으로 보고 run 중단
            if chunk_403 >= max(10, len(chunk) // 2):
                db.add_system_log(
                    f"[classify] {group_id}: http_403 {chunk_403}/{len(chunk)} — "
                    f"Akamai velocity 차단 감지, run 중단 (쿨다운 후 재트리거 시 resume)"
                )
                return {"status": "blocked", "group_id": group_id,
                        "classified": len(classified), "failed": len(failed),
                        "chunk_403": chunk_403}

    # 최종 저장 (in_progress=False)
    full = _save_partial(is_final=True)

    by_type = Counter(c["page_type"] for c in classified.values())
    took = (datetime.now(timezone.utc) - started_at).total_seconds()
    db.add_system_log(
        f"[classify] {group_id}: 완료 — total={len(urls)} classified={len(classified)} "
        f"failed={len(failed)} took={took:.0f}s by_type={dict(by_type.most_common(5))}"
    )

    # peer(Mac Mini)로 즉시 push — 최종 결과만
    try:
        from main import _peer_push_aux
        await _peer_push_aux("/admin/url-classifications-raw", full)
    except Exception as e:
        log.warning("peer push 실패 (무시): %s", e)

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


def cleanup_excluded_in_group(group_id: str) -> dict:
    """기존 분류 결과에서 _EXCLUDE_URL_PATTERN에 매칭되는 URL을 제거.

    business 같은 패턴을 새로 추가했을 때 한 번 호출해서 dirty entry 청소.
    summary/by_type/failed_urls 모두 재계산.
    """
    full = load_classifications_all()
    if group_id not in full:
        return {"status": "not_found", "group_id": group_id}

    entry = full[group_id]
    classifications = entry.get("classifications", {})
    failed_urls = entry.get("failed_urls", [])

    before_c = len(classifications)
    before_f = len(failed_urls)

    new_classifications = {u: v for u, v in classifications.items() if not _EXCLUDE_URL_PATTERN.search(u)}
    new_failed = [f for f in failed_urls if isinstance(f, dict) and not _EXCLUDE_URL_PATTERN.search(f.get("url", ""))]

    removed_c = before_c - len(new_classifications)
    removed_f = before_f - len(new_failed)

    by_type = Counter(c["page_type"] for c in new_classifications.values())
    entry["classifications"] = new_classifications
    entry["failed_urls"]     = new_failed
    entry["summary"] = {
        **entry.get("summary", {}),
        "classified": len(new_classifications),
        "failed":     len(new_failed),
        "by_type":    dict(by_type.most_common()),
    }
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    full[group_id] = entry

    with open(CLASSIFICATIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(full, f, ensure_ascii=False, indent=2)

    return {
        "status":            "ok",
        "group_id":          group_id,
        "removed_classified": removed_c,
        "removed_failed":    removed_f,
        "remaining":         len(new_classifications),
    }
