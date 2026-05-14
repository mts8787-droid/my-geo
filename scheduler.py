"""
정기 Audit 실행기.

audit_data.json의 schedules 정의를 읽어 APScheduler AsyncIOScheduler에 등록한다.
실행 결과(요약)는 audit_data.json::runs에 누적 저장 (최근 50개 유지).

스케줄 정의 예:
    {
      "id": "sch_1",
      "name": "메인 페이지 일일 점검",
      "group_id": "grp_main",
      "frequency": "daily" | "weekly" | "monthly",
      "time": "09:00",
      "enabled": true
    }

실행 결과 예 (audit_data.json::runs[]):
    {
      "id": "run_<uuid>",
      "schedule_id": "sch_1",
      "schedule_name": "메인 페이지 일일 점검",
      "group_id": "grp_main",
      "started_at": "2026-05-07T09:00:00+00:00",
      "finished_at": "...",
      "status": "ok|partial|error",
      "url_count": 5,
      "success_count": 4,
      "summary": [{"url": "...", "score": 87, "grade": "Need Improvement"} ...],
      "error": null
    }
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    APSCHEDULER_AVAILABLE = True
except Exception:
    AsyncIOScheduler = None
    CronTrigger = None
    APSCHEDULER_AVAILABLE = False

import audit_store
import db

log = logging.getLogger("geo_audit.scheduler")
_scheduler: Optional["AsyncIOScheduler"] = None
_MAX_RUNS = 50  # 가장 최근 N개만 보관


def _cron_trigger_for_schedule(sch: dict):
    """schedule dict → CronTrigger.

    frequency:
      - daily   → 매일 HH:MM
      - weekly  → 매주 월요일 HH:MM
      - monthly → 매월 1일 HH:MM
    """
    freq = (sch.get("frequency") or "daily").lower()
    time_str = sch.get("time") or "09:00"
    try:
        hour, minute = [int(x) for x in time_str.split(":")[:2]]
    except Exception:
        hour, minute = 9, 0

    if freq == "weekly":
        return CronTrigger(day_of_week="mon", hour=hour, minute=minute)
    if freq == "monthly":
        return CronTrigger(day=1, hour=hour, minute=minute)
    return CronTrigger(hour=hour, minute=minute)


async def _run_schedule(schedule_id: str, force: bool = False):
    """스케줄 실행 — 그룹의 URL을 일괄 분석 후 결과 저장."""
    from analyzer import analyze_url  # 순환 import 회피

    data = audit_store.load()
    sch = next((s for s in data["schedules"] if s.get("id") == schedule_id), None)
    if not sch or (not force and not sch.get("enabled", True)):
        return
    group = next((g for g in data["groups"] if g.get("id") == sch.get("group_id")), None)
    urls = (group or {}).get("urls", [])
    csv_file = (group or {}).get("csv_file")
    if csv_file and not urls:
        try:
            import csv
            with open(csv_file, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                urls = [row["URL"] for row in reader if "URL" in row]
        except Exception as e:
            log.error(f"Failed to load CSV for group {group.get('name')}: {e}")
            urls = []
            
    chunk_size = sch.get("chunk_size", 0)
    chunk_index = sch.get("chunk_index", 0)
    # 슬라이스 전 전체 URL 수 보관 (청크 종료 판단에 사용)
    total_urls = (group or {}).get("url_count") or len(urls)
    if chunk_size > 0 and urls:
        start = chunk_index * chunk_size
        end = start + chunk_size
        urls = urls[start:end]

    run = {
        "id":            f"run_{uuid.uuid4().hex[:12]}",
        "schedule_id":   schedule_id,
        "schedule_name": sch.get("name", schedule_id),
        "group_id":      sch.get("group_id"),
        "group_name":    (group or {}).get("name"),
        "started_at":    datetime.now(timezone.utc).isoformat(),
        "url_count":     len(urls),
        "success_count": 0,
        "status":        "running",
        "summary":       [],
        "error":         None,
    }

    if not urls:
        run["status"] = "error"
        run["error"]  = "그룹에 URL 없음"
        run["finished_at"] = datetime.now(timezone.utc).isoformat()
        db.save_schedule_run(run)
        return

    sem = asyncio.Semaphore(5)

    async def _run_one(url: str) -> dict:
        async with sem:
            try:
                result = await analyze_url(url, lightweight=True)
                score = (result.get("score") or {})
                return {
                    "url":   url,
                    "score": score.get("total"),
                    "grade": score.get("grade"),
                }
            except Exception as e:
                return {"url": url, "error": f"{type(e).__name__}: {str(e)[:120]}"}

    results = await asyncio.gather(*(_run_one(u) for u in urls), return_exceptions=False)
    success_count = sum(1 for r in results if "error" not in r and r.get("score") is not None)
    run["summary"]       = results
    run["success_count"] = success_count
    run["status"]        = "ok" if success_count == len(urls) else ("partial" if success_count else "error")
    run["finished_at"]   = datetime.now(timezone.utc).isoformat()

    # 결과 저장 — DB에 저장
    db.save_schedule_run(run)

    # Update schedule chunk_index if chunking is used
    fresh = audit_store.load()
    if chunk_size > 0:
        fresh_sch = next((s for s in fresh.get("schedules", []) if s.get("id") == schedule_id), None)
        if fresh_sch:
            if (chunk_index + 1) * chunk_size >= total_urls:
                fresh_sch["chunk_index"] = 0
                fresh_sch["enabled"] = False # Disable when done
            else:
                fresh_sch["chunk_index"] = chunk_index + 1

    await audit_store.save(fresh)


def reload_schedules() -> int:
    """audit_data.json::schedules를 읽어 스케줄러에 재등록.

    호출 가능한 시점:
      - 서버 startup
      - 어드민에서 스케줄 추가/수정/삭제 후

    Returns: 등록된 활성 스케줄 개수
    """
    global _scheduler
    if not APSCHEDULER_AVAILABLE or _scheduler is None:
        return 0
    _scheduler.remove_all_jobs()
    data = audit_store.load()
    count = 0
    for sch in data.get("schedules", []):
        if not sch.get("enabled", True) or not sch.get("id"):
            continue
        try:
            trigger = _cron_trigger_for_schedule(sch)
            # AsyncIOScheduler는 코루틴 함수를 직접 받는다 — 래퍼 불필요 (#7)
            _scheduler.add_job(
                _run_schedule,
                trigger=trigger,
                args=[sch["id"]],
                id=f"sch_{sch['id']}",
                replace_existing=True,
                misfire_grace_time=300,
            )
            count += 1
        except Exception as e:
            log.exception("'%s' 등록 실패: %s", sch.get("name"), e)
    return count


def start_scheduler() -> bool:
    """서버 startup에서 호출. APScheduler 시작 + 스케줄 로드."""
    global _scheduler
    if not APSCHEDULER_AVAILABLE:
        log.warning("APScheduler 미설치 — 정기 Audit 비활성")
        return False
    if _scheduler is not None and getattr(_scheduler, "running", False):
        return True
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.start()
    n = reload_schedules()
    log.info("scheduler 시작 — 활성 스케줄 %d개", n)
    return True


def shutdown_scheduler() -> None:
    """서버 shutdown에서 호출."""
    global _scheduler
    if _scheduler is not None and getattr(_scheduler, "running", False):
        _scheduler.shutdown(wait=False)
        log.info("scheduler 정지")


async def trigger_now(schedule_id: str) -> dict:
    """수동 즉시 실행 — 어드민 UI '지금 실행' 버튼용."""
    await _run_schedule(schedule_id, force=True)
    runs = db.get_recent_schedule_runs(schedule_id=schedule_id, limit=1)
    return runs[0] if runs else {"status": "error", "error": "결과 없음"}


def get_recent_runs(schedule_id: Optional[str] = None, limit: int = 20) -> List[dict]:
    """최근 실행 결과 조회 — 어드민 표시용."""
    return db.get_recent_schedule_runs(schedule_id=schedule_id, limit=limit)
