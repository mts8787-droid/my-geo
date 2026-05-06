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
import json as _json
import os
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

_AUDIT_DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_data.json")
_data_lock = asyncio.Lock()
_scheduler: Optional["AsyncIOScheduler"] = None
_MAX_RUNS = 50  # 가장 최근 N개만 보관


def _load() -> dict:
    try:
        with open(_AUDIT_DATA_PATH, "r", encoding="utf-8") as f:
            data = _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError):
        data = {}
    data.setdefault("groups", [])
    data.setdefault("schedules", [])
    data.setdefault("runs", [])
    # 마이그레이션: id가 없는 그룹/스케줄에 안정 ID 부여 (어드민 reorder/삭제에 견고)
    mutated = False
    for g in data["groups"]:
        if "id" not in g:
            g["id"] = f"grp_{uuid.uuid4().hex[:10]}"
            mutated = True
    for s in data["schedules"]:
        if "id" not in s:
            s["id"] = f"sch_{uuid.uuid4().hex[:10]}"
            mutated = True
        # 기존 키 호환: freq → frequency 정규화 (양쪽 다 보존)
        if "freq" in s and "frequency" not in s:
            s["frequency"] = s["freq"]
            mutated = True
        # groupIdx → group_id 변환
        if "groupIdx" in s and "group_id" not in s:
            try:
                idx = int(s["groupIdx"])
                if 0 <= idx < len(data["groups"]):
                    s["group_id"] = data["groups"][idx]["id"]
                    mutated = True
            except Exception:
                pass
    if mutated:
        # 동기 저장 (호출자가 await할 수 없는 컨텍스트에서도 안전)
        try:
            with open(_AUDIT_DATA_PATH, "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[scheduler] migration save failed: {e}")
    return data


async def _save(data: dict) -> None:
    async with _data_lock:
        with open(_AUDIT_DATA_PATH, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)


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


async def _run_schedule(schedule_id: str):
    """스케줄 실행 — 그룹의 URL을 일괄 분석 후 결과 저장."""
    from analyzer import analyze_url  # 순환 import 회피

    data = _load()
    sch = next((s for s in data["schedules"] if s.get("id") == schedule_id), None)
    if not sch or not sch.get("enabled", True):
        return
    group = next((g for g in data["groups"] if g.get("id") == sch.get("group_id")), None)
    urls = (group or {}).get("urls", [])

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
        data["runs"].insert(0, run)
        data["runs"] = data["runs"][:_MAX_RUNS]
        await _save(data)
        return

    # 동시 실행 제한 (analyzer 내부 세마포어와 별개로 스케줄러 레벨에서도 5개 제한)
    sem = asyncio.Semaphore(5)

    async def _run_one(url: str) -> dict:
        async with sem:
            try:
                # 정기 점검은 lightweight 모드 (Playwright 생략, 메모리 절약)
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

    # 결과 저장 — 다른 코드(예: admin update)와의 race를 줄이려 최신 디스크 상태 다시 로드
    fresh = _load()
    fresh.setdefault("runs", []).insert(0, run)
    fresh["runs"] = fresh["runs"][:_MAX_RUNS]
    await _save(fresh)


def _job_factory(schedule_id: str):
    """APScheduler 작업으로 등록할 콜러블."""
    def _job():
        # AsyncIOScheduler는 코루틴을 직접 받을 수 있지만, 명시적으로 task 생성
        loop = asyncio.get_event_loop()
        loop.create_task(_run_schedule(schedule_id))
    return _job


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
    # 기존 작업 모두 제거 후 재등록 (간단/안전)
    _scheduler.remove_all_jobs()
    data = _load()
    count = 0
    for sch in data.get("schedules", []):
        if not sch.get("enabled", True) or not sch.get("id"):
            continue
        try:
            trigger = _cron_trigger_for_schedule(sch)
            _scheduler.add_job(
                _job_factory(sch["id"]),
                trigger=trigger,
                id=f"sch_{sch['id']}",
                replace_existing=True,
                misfire_grace_time=300,
            )
            count += 1
        except Exception as e:
            print(f"[scheduler] '{sch.get('name')}' 등록 실패: {e}")
    return count


def start_scheduler() -> bool:
    """서버 startup에서 호출. APScheduler 시작 + 스케줄 로드."""
    global _scheduler
    if not APSCHEDULER_AVAILABLE:
        print("[scheduler] APScheduler 미설치 — 정기 Audit 비활성")
        return False
    if _scheduler is not None and getattr(_scheduler, "running", False):
        return True
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.start()
    n = reload_schedules()
    print(f"[scheduler] 시작 완료 — 활성 스케줄 {n}개")
    return True


def shutdown_scheduler() -> None:
    """서버 shutdown에서 호출."""
    global _scheduler
    if _scheduler is not None and getattr(_scheduler, "running", False):
        _scheduler.shutdown(wait=False)
        print("[scheduler] 정지")


async def trigger_now(schedule_id: str) -> dict:
    """수동 즉시 실행 — 어드민 UI '지금 실행' 버튼용."""
    await _run_schedule(schedule_id)
    fresh = _load()
    last = next((r for r in fresh.get("runs", []) if r.get("schedule_id") == schedule_id), None)
    return last or {"status": "error", "error": "결과 없음"}


def get_recent_runs(schedule_id: Optional[str] = None, limit: int = 20) -> List[dict]:
    """최근 실행 결과 조회 — 어드민 표시용."""
    data = _load()
    runs = data.get("runs", [])
    if schedule_id:
        runs = [r for r in runs if r.get("schedule_id") == schedule_id]
    return runs[:limit]
