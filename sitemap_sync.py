"""사이트맵 일일 동기화.

audit_data.json::groups 중 sitemap_url이 등록된 그룹들을 순회하면서:
  1. 사이트맵을 파싱해 최신 URL 셋을 가져온다
  2. 그룹의 기존 urls와 diff (추가/삭제)
  3. 변화가 있으면 audit_data.json에 반영 + Ollama로 한국어 요약 로그
  4. db.system_logs에 결과 기록 (어드민 로그창에 표시됨)
"""
import asyncio
import logging
from datetime import datetime
from typing import Dict, List

import audit_store
import db
import ollama_client
from sitemap_agent import parse_sitemap

log = logging.getLogger("geo_audit.sitemap_sync")

# Ollama 요약 프롬프트
_SYS_PROMPT = (
    "너는 LG 웹사이트의 사이트맵 변경을 모니터링하는 분석가다. "
    "사용자가 그룹별 URL 추가·삭제 통계를 주면, 한국어 한 문장으로 핵심을 요약해라. "
    "예: '신규 12개(대부분 PDP), 제거 3개(구형 모델)'. 30자 이내."
)


def _to_url_str(entry) -> str:
    """audit_data.json의 urls는 문자열 리스트 또는 {url, lastScore, lastRun} 객체 리스트일 수 있다."""
    if isinstance(entry, dict):
        return entry.get("url", "") or ""
    return str(entry) if entry else ""


async def _summarize_change(group_name: str, added: List[str], removed: List[str]) -> str:
    """Ollama로 변화를 짧게 요약. 실패 시 단순 통계 문자열 반환."""
    fallback = f"신규 {len(added)}, 제거 {len(removed)}"
    if not ollama_client.is_enabled():
        return fallback
    sample_added = added[:5]
    sample_removed = removed[:5]
    prompt = (
        f"그룹: {group_name}\n"
        f"신규 추가 URL ({len(added)}개) 샘플:\n" + "\n".join(sample_added) + "\n\n"
        f"삭제된 URL ({len(removed)}개) 샘플:\n" + "\n".join(sample_removed)
    )
    summary = await ollama_client.chat(prompt, system=_SYS_PROMPT)
    return summary or fallback


async def sync_group(group: dict) -> Dict[str, int]:
    """단일 그룹 동기화. 변경 통계 dict 반환."""
    name = group.get("name", "(이름없음)")
    sitemap_url = group.get("sitemap_url")
    if not sitemap_url:
        return {"skipped": 1, "added": 0, "removed": 0}

    try:
        latest = await parse_sitemap(sitemap_url)
    except Exception as e:
        db.add_system_log(f"[sitemap-sync] {name}: 사이트맵 파싱 실패 — {e}")
        return {"error": 1, "added": 0, "removed": 0}

    raw_urls = group.get("urls") or []
    sample = raw_urls[0] if raw_urls else None
    current_strs = [s for s in (_to_url_str(u) for u in raw_urls) if s]
    latest_set = set(latest)
    current_set = set(current_strs)
    added = sorted(latest_set - current_set)
    removed = sorted(current_set - latest_set)

    if not added and not removed:
        return {"unchanged": 1, "added": 0, "removed": 0}

    # 사이트맵을 source of truth로 갱신. 기존 데이터 포맷(문자열 vs 객체)을 보존한다.
    new_urls = sorted(latest_set)
    if isinstance(sample, dict):
        group["urls"] = [{"url": u, "lastScore": None, "lastRun": None} for u in new_urls]
    else:
        group["urls"] = new_urls
    group["url_count"] = len(new_urls)

    summary = await _summarize_change(name, added, removed)
    db.add_system_log(f"[sitemap-sync] {name}: {summary}")
    return {"added": len(added), "removed": len(removed)}


async def run_daily_sync() -> dict:
    """모든 그룹 순회. 스케줄러가 매일 호출."""
    started = datetime.utcnow().isoformat()
    data = audit_store.load()
    groups = data.get("groups") or []

    total = {"groups_total": len(groups), "groups_synced": 0, "added": 0, "removed": 0, "skipped": 0, "errors": 0}
    sync_targets = [g for g in groups if g.get("sitemap_url")]

    if not sync_targets:
        db.add_system_log("[sitemap-sync] 동기 대상 그룹 없음 (sitemap_url 등록된 그룹 없음)")
        return {**total, "started_at": started, "finished_at": datetime.utcnow().isoformat()}

    db.add_system_log(f"[sitemap-sync] 시작 — {len(sync_targets)}개 그룹")

    for group in sync_targets:
        try:
            stat = await sync_group(group)
            total["groups_synced"] += 1
            total["added"] += stat.get("added", 0)
            total["removed"] += stat.get("removed", 0)
            total["skipped"] += stat.get("skipped", 0)
            total["errors"] += stat.get("error", 0)
        except Exception as e:
            log.exception("sync_group 예외: %s", e)
            db.add_system_log(f"[sitemap-sync] {group.get('name')}: 예외 — {e}")
            total["errors"] += 1

    # 변경 사항 한 번에 저장 (그룹들 객체가 같은 data dict에 참조됨)
    await audit_store.save(data)

    finished = datetime.utcnow().isoformat()
    db.add_system_log(
        f"[sitemap-sync] 완료 — 그룹 {total['groups_synced']}/{total['groups_total']}, "
        f"추가 {total['added']}, 제거 {total['removed']}, 에러 {total['errors']}"
    )
    return {**total, "started_at": started, "finished_at": finished}
