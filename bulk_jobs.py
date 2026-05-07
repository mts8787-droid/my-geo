"""
In-memory bulk audit 작업 큐 (#18).

`/analyze-bulk`는 1000개 URL을 한 요청 내 처리하다 클라이언트 timeout이 나기 쉽다.
이 모듈은 process-local 메모리에 job 상태를 보관하고, 클라이언트가 polling으로
진행률을 확인하도록 한다. Redis 등 외부 의존성 없이 작동하므로 단일 프로세스
배포(현재 Render 구성)에서 충분하다.

API:
    job_id = submit(urls, scope, lightweight=True)  # 즉시 반환, 백그라운드 진행
    status = get(job_id)                            # 진행률/완료 항목/상태
    cancel(job_id)                                  # 진행 중인 task 취소

상태 형식:
    {
      "id": "...", "scope": "all", "status": "running|done|cancelled|error",
      "total": 100, "completed": 42, "items": [...완료된 결과들...],
      "average": 87.3 (scope=all 시), "started_at": iso, "finished_at": iso|None,
    }

보관 정책: 최근 50개 job. 그 이전은 제거 (메모리 보호).
"""
import asyncio
import uuid
from datetime import datetime, timezone
from typing import List, Optional

_MAX_JOBS = 50
_jobs: dict = {}            # job_id → status dict
_tasks: dict = {}           # job_id → asyncio.Task
_lock = asyncio.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trim_old_jobs() -> None:
    if len(_jobs) <= _MAX_JOBS:
        return
    # 종료된 것부터 오래된 순으로 제거
    finished = [jid for jid, j in _jobs.items() if j.get("status") in ("done", "cancelled", "error")]
    finished.sort(key=lambda jid: _jobs[jid].get("finished_at") or "")
    for jid in finished[: len(_jobs) - _MAX_JOBS]:
        _jobs.pop(jid, None)


async def _run(job_id: str, urls: List[str], scope: str, lightweight: bool) -> None:
    from analyzer import analyze_url  # 순환 import 회피
    job = _jobs[job_id]
    sem = asyncio.Semaphore(5)

    async def _one(url: str) -> dict:
        async with sem:
            try:
                return {"url": url, "result": await analyze_url(url, lightweight=lightweight, scope=scope), "error": None}
            except asyncio.CancelledError:
                raise
            except Exception as e:
                return {"url": url, "result": None, "error": f"{type(e).__name__}: {str(e)[:120]}"}

    try:
        # 항목별로 progress 갱신 — 클라이언트 polling이 매 5개 단위로 의미 있는 갱신을 받도록
        for i in range(0, len(urls), 5):
            batch = urls[i:i + 5]
            results = await asyncio.gather(*(_one(u) for u in batch))
            async with _lock:
                if job.get("status") == "cancelled":
                    return
                job["items"].extend(results)
                job["completed"] = len(job["items"])
        async with _lock:
            if scope == "all":
                scores = [it["result"]["score"]["total"] for it in job["items"] if it["result"]]
                job["average"] = round(sum(scores) / len(scores), 1) if scores else 0
            job["status"] = "done"
            job["finished_at"] = _now()
    except asyncio.CancelledError:
        async with _lock:
            job["status"] = "cancelled"
            job["finished_at"] = _now()
        raise
    except Exception as e:
        async with _lock:
            job["status"] = "error"
            job["error"] = f"{type(e).__name__}: {e}"
            job["finished_at"] = _now()


def submit(urls: List[str], scope: str = "all", lightweight: bool = True) -> str:
    """새 bulk job 제출. 즉시 job_id 반환."""
    job_id = f"bulk_{uuid.uuid4().hex[:12]}"
    _jobs[job_id] = {
        "id":          job_id,
        "scope":       scope,
        "status":      "running",
        "total":       len(urls),
        "completed":   0,
        "items":       [],
        "average":     None,
        "error":       None,
        "started_at":  _now(),
        "finished_at": None,
    }
    _trim_old_jobs()
    _tasks[job_id] = asyncio.create_task(_run(job_id, urls, scope, lightweight))
    return job_id


def get(job_id: str) -> Optional[dict]:
    return _jobs.get(job_id)


def cancel(job_id: str) -> bool:
    task = _tasks.get(job_id)
    if task and not task.done():
        task.cancel()
        return True
    return False
