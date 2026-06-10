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
      - daily         → 매일 HH:MM
      - weekly        → 매주 월요일 HH:MM
      - monthly       → 매월 1일 HH:MM
      - hourly        → 매 시 정각 (HH:00)
      - every_30_min  → 매 30분 (HH:00, HH:30) — time 무시
      - every_15_min  → 매 15분 (HH:00, HH:15, HH:30, HH:45)
    """
    freq = (sch.get("frequency") or "daily").lower()
    time_str = sch.get("time") or "09:00"
    try:
        hour, minute = [int(x) for x in time_str.split(":")[:2]]
    except Exception:
        hour, minute = 9, 0

    if freq == "every_15_min" or freq == "15min":
        return CronTrigger(minute="0,15,30,45")
    if freq == "every_30_min" or freq == "30min":
        return CronTrigger(minute="0,30")
    if freq == "hourly":
        return CronTrigger(minute="0")
    if freq == "weekly":
        return CronTrigger(day_of_week="mon", hour=hour, minute=minute)
    if freq == "monthly":
        return CronTrigger(day=1, hour=hour, minute=minute)
    if freq.startswith("monthly_day_"):
        try:
            day = int(freq.split("_")[-1])
            return CronTrigger(day=day, hour=hour, minute=minute)
        except Exception:
            return CronTrigger(day=1, hour=hour, minute=minute)
    if freq == "semimonthly":
        # 매월 2회 — 1일은 baseline cron과 겹치지 않게 2·16일 사용
        return CronTrigger(day="2,16", hour=hour, minute=minute)
    return CronTrigger(hour=hour, minute=minute)


async def _analyze_urls(sch_label: str, urls: List[str]):
    """URL 목록을 동시성 5로 분석. (results, success_count) 반환."""
    from analyzer import analyze_url  # 순환 import 회피

    sem = asyncio.Semaphore(5)

    async def _run_one(url: str) -> dict:
        async with sem:
            db.add_system_log(f"[audit] {sch_label}: 처리 중 {url}")
            try:
                result = await analyze_url(url, lightweight=True)
                score = (result.get("score") or {})
                total = score.get("total")
                grade = score.get("grade")
                db.add_system_log(f"[audit] {sch_label}: {url} → total={total} ({grade})")
                # analyze_url의 전체 결과를 그대로 보존 (49 항목 breakdown 포함)
                return {"url": url, "result": result}
            except Exception as e:
                err = f"{type(e).__name__}: {str(e)[:200]}"
                db.add_system_log(f"[audit] {sch_label}: {url} → ERROR {err}")
                return {"url": url, "error": err}

    results = await asyncio.gather(*(_run_one(u) for u in urls), return_exceptions=False)
    success_count = sum(
        1 for r in results
        if "error" not in r and (r.get("result", {}).get("score") or {}).get("total") is not None
    )
    return results, success_count


async def _run_audit_lists_schedule(sch: dict, data: dict):
    """audit_lists 기반 실행 — 국가(그룹)를 이름순으로 순차 처리, 국가별 run 레코드 저장."""
    schedule_id = sch.get("id")
    sch_label = sch.get("name", schedule_id)

    by_group: dict = {}
    for lst in data.get("audit_lists", []):
        gid = lst.get("source_group_id")
        if gid:
            by_group.setdefault(gid, []).extend(lst.get("urls") or [])
    if not by_group:
        db.add_system_log(f"[audit] {sch_label}: audit_lists 비어있음 — skip")
        return

    group_names = {g.get("id"): g.get("name") for g in data.get("groups", [])}
    order = sorted(by_group, key=lambda gid: group_names.get(gid) or gid)
    db.add_system_log(
        f"[audit] {sch_label}: {len(order)}개 국가 순차 실행 시작 — "
        + ", ".join(group_names.get(g) or g for g in order)
    )

    for gid in order:
        urls = by_group[gid]
        gname = group_names.get(gid) or gid
        run = {
            "id":            f"run_{uuid.uuid4().hex[:12]}",
            "schedule_id":   schedule_id,
            "schedule_name": sch_label,
            "group_id":      gid,
            "group_name":    gname,
            "started_at":    datetime.now(timezone.utc).isoformat(),
            "url_count":     len(urls),
            "success_count": 0,
            "status":        "running",
            "summary":       [],
            "error":         None,
        }
        results, success_count = await _analyze_urls(f"{sch_label}/{gname}", urls)
        run["summary"]       = results
        run["success_count"] = success_count
        run["status"]        = "ok" if success_count == len(urls) else ("partial" if success_count else "error")
        run["finished_at"]   = datetime.now(timezone.utc).isoformat()
        db.save_schedule_run(run)
        db.add_system_log(f"[audit] {sch_label}: {gname} 완료 — {success_count}/{len(urls)}")


async def _run_schedule(schedule_id: str, force: bool = False):
    """스케줄 실행 — 그룹의 URL을 일괄 분석 후 결과 저장."""
    data = audit_store.load()
    sch = next((s for s in data["schedules"] if s.get("id") == schedule_id), None)
    if not sch or (not force and not sch.get("enabled", True)):
        return
    if sch.get("use_audit_lists"):
        await _run_audit_lists_schedule(sch, data)
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
        # chunk_index가 범위를 벗어났으면 0으로 리셋 (무한 빈-slice 루프 회피)
        if chunk_index * chunk_size >= len(urls):
            db.add_system_log(
                f"[audit] {sch.get('name')}: chunk_index={chunk_index} 범위 벗어남 "
                f"(start={chunk_index*chunk_size} >= total={len(urls)}) → 0 으로 리셋"
            )
            chunk_index = 0
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

    group_name = (group or {}).get("name", "?")
    if not urls:
        run["status"] = "error"
        run["error"]  = "그룹에 URL 없음"
        run["finished_at"] = datetime.now(timezone.utc).isoformat()
        db.save_schedule_run(run)
        db.add_system_log(f"[audit] {sch.get('name')} ({group_name}): 그룹에 URL 없음 — skip")
        return

    sch_label = sch.get("name", schedule_id)
    results, success_count = await _analyze_urls(sch_label, urls)
    run["summary"]       = results  # 풀 결과 — 49항목 breakdown 포함
    run["success_count"] = success_count
    run["status"]        = "ok" if success_count == len(urls) else ("partial" if success_count else "error")
    run["finished_at"]   = datetime.now(timezone.utc).isoformat()

    # 결과 저장 — DB에 저장 (chunk-level 시작/완료 로그는 더 이상 안 찍음 — per-URL 로그가 대신)
    db.save_schedule_run(run)

    # chunk_index 갱신 — 마지막 chunk 끝나면 0으로 리셋하여 다음 발화 시 자동 재사이클.
    # (이전엔 enabled=False로 자동 비활성 → 정기 sync에 부적합해서 제거)
    fresh = audit_store.load()
    if chunk_size > 0:
        fresh_sch = next((s for s in fresh.get("schedules", []) if s.get("id") == schedule_id), None)
        if fresh_sch:
            if (chunk_index + 1) * chunk_size >= total_urls:
                fresh_sch["chunk_index"] = 0   # 다음 발화 시 처음부터 새 사이클
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
    import os
    global _scheduler
    if not APSCHEDULER_AVAILABLE or _scheduler is None:
        return 0
    _scheduler.remove_all_jobs()
    # remove_all_jobs가 sitemap-sync / csr-baseline job도 지우므로 재등록
    _register_sitemap_sync_job()
    _register_csr_baseline_job()
    # Mac Mini daemon 등에서 중복 실행 방지 — env로 audit 스케줄 등록 차단
    if os.environ.get("AUDIT_SCHEDULES_ENABLED", "true").lower() in ("false", "0", "no"):
        log.info("AUDIT_SCHEDULES_ENABLED=false — audit 스케줄 미등록")
        return 0
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


_SITEMAP_SYNC_JOB_ID = "sitemap_sync_daily"


def _register_sitemap_sync_job() -> None:
    """매일 1회 사이트맵 동기 job 등록.

    - SITEMAP_SYNC_ENABLED=false 면 등록 skip (Mac Mini와 Render에서 중복 실행 방지용)
    - SITEMAP_SYNC_CRON env로 시간 조정 가능 (기본 21:00 UTC = 06:00 KST)
    """
    import os
    if _scheduler is None:
        return
    if os.environ.get("SITEMAP_SYNC_ENABLED", "true").lower() in ("false", "0", "no"):
        log.info("SITEMAP_SYNC_ENABLED=false — 사이트맵 sync job 미등록")
        return
    cron_spec = os.environ.get("SITEMAP_SYNC_CRON", "")
    try:
        if cron_spec:
            # 형식: "minute hour" (예: "0 21")
            parts = cron_spec.split()
            minute = int(parts[0])
            hour = int(parts[1])
        else:
            minute, hour = 0, 21
        trigger = CronTrigger(hour=hour, minute=minute)
    except Exception:
        trigger = CronTrigger(hour=21, minute=0)

    async def _job():
        from sitemap_sync import run_daily_sync
        try:
            await run_daily_sync()
        except Exception as e:
            log.exception("sitemap_sync 실행 실패: %s", e)

    _scheduler.add_job(
        _job,
        trigger=trigger,
        id=_SITEMAP_SYNC_JOB_ID,
        replace_existing=True,
        misfire_grace_time=3600,
    )


_CSR_BASELINE_JOB_ID = "csr_baseline_monthly"


def _register_csr_baseline_job() -> None:
    """매월 1일 02:00 UTC 에 page_type별 CSR/SSR baseline 갱신.

    - 12개 page_type × 10개 URL = 약 120 URL을 Playwright(lightweight=False)로 실측
    - 평균을 data/csr_baseline.json에 저장
    - 일반 sitemap audit(lightweight=True)에서 csr_ratio가 비어있을 때 이 평균값 주입
    """
    if _scheduler is None:
        return
    trigger = CronTrigger(day=1, hour=2, minute=0)

    async def _job():
        from csr_baseline import regenerate_baseline
        try:
            result = await regenerate_baseline()
            log.info("csr_baseline 갱신 완료: %s", result)
        except Exception as e:
            log.exception("csr_baseline 갱신 실패: %s", e)

    _scheduler.add_job(
        _job,
        trigger=trigger,
        id=_CSR_BASELINE_JOB_ID,
        replace_existing=True,
        misfire_grace_time=7200,
    )


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
    _register_sitemap_sync_job()
    _register_csr_baseline_job()
    log.info("scheduler 시작 — 활성 스케줄 %d개 + sitemap-sync daily + csr-baseline monthly", n)
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
